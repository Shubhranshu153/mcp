"""Finch MCP Server main module.

This module provides the MCP server implementation for Finch container operations.
"""

import logging
import os
import re
import sys
from awslabs.finch_mcp_server.consts import LOG_FILE, SERVER_NAME

# Import Pydantic models for input validation
from awslabs.finch_mcp_server.models import (
    BuildImageRequest,
    CreateEcrRepoRequest,
    PushImageRequest,
    Result,
)
from awslabs.finch_mcp_server.utils.build import build_image, contains_ecr_reference
from awslabs.finch_mcp_server.utils.common import format_result
from awslabs.finch_mcp_server.utils.ecr import create_ecr_repository

# Import utility functions from local modules
from awslabs.finch_mcp_server.utils.push import is_ecr_repository, push_image
from awslabs.finch_mcp_server.utils.vm import (
    check_finch_installation,
    configure_ecr,
    get_vm_status,
    initialize_vm,
    is_vm_nonexistent,
    is_vm_running,
    is_vm_stopped,
    start_stopped_vm,
    stop_vm,
)
from logging.handlers import RotatingFileHandler
from mcp.server.fastmcp import FastMCP
from typing import Any, Dict


class SensitiveDataFilter(logging.Filter):
    """Filter that redacts sensitive information from log messages."""

    def __init__(self):
        """Initialize the filter with regex patterns for sensitive data detection.

        Sets up regular expression patterns to identify and redact various types of
        sensitive information such as API keys, passwords, and credentials.
        """
        super().__init__()
        self.patterns = [
            (re.compile(r'((?<![A-Z0-9])[A-Z0-9]{20}(?![A-Z0-9]))'), 'AWS_ACCESS_KEY_REDACTED'),
            (
                re.compile(r'((?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=]))'),
                'AWS_SECRET_KEY_REDACTED',
            ),
            (re.compile(r'api[_-]?key[=:]\s*["\'](.*?)["\']', re.IGNORECASE), 'api_key=REDACTED'),
            (re.compile(r'password[=:]\s*["\'](.*?)["\']', re.IGNORECASE), 'password=REDACTED'),
            (re.compile(r'secret[=:]\s*["\'](.*?)["\']', re.IGNORECASE), 'secret=REDACTED'),
            (re.compile(r'token[=:]\s*["\'](.*?)["\']', re.IGNORECASE), 'token=REDACTED'),
            (re.compile(r'(https?://)([^:@\s]+):([^:@\s]+)@'), r'\1REDACTED:REDACTED@'),
        ]

    def filter(self, record):
        """Process the log record to redact sensitive information.

        Args:
            record: The log record to process

        Returns:
            bool: Always returns True to allow the log record to be processed

        """
        if isinstance(record.msg, str):
            for pattern, replacement in self.patterns:
                record.msg = pattern.sub(replacement, record.msg)

        if record.args:
            args_list = list(record.args)
            for i, arg in enumerate(args_list):
                if isinstance(arg, str):
                    for pattern, replacement in self.patterns:
                        args_list[i] = pattern.sub(replacement, arg)
            record.args = tuple(args_list)

        return True


log_level = logging.INFO
server_log_level = os.environ.get('SERVER_LOG_LEVEL', '').upper()
if server_log_level and hasattr(logging, server_log_level):
    log_level = getattr(logging, server_log_level)

handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=7)

# Add sensitive data filter to the handler
sensitive_filter = SensitiveDataFilter()
handler.addFilter(sensitive_filter)

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[handler],
)
logger = logging.getLogger(SERVER_NAME)

mcp = FastMCP(SERVER_NAME)


def ensure_vm_running() -> Dict[str, Any]:
    """Ensure that the Finch VM is running before performing operations.

    This function checks the current status of the Finch VM and takes appropriate action:
    - If the VM is nonexistent: Creates a new VM instance using 'finch vm init'
    - If the VM is stopped: Starts the VM using 'finch vm start'
    - If the VM is already running: Does nothing

    Returns:
        Dict[str, Any]: A dictionary containing:
            - status (str): "success" if the VM is running or was started successfully,
                            "error" otherwise
            - message (str): A descriptive message about the result of the operation

    """
    try:
        if sys.platform == 'linux':
            logger.debug('Linux OS detected. No VM operation required')
            return format_result('success', 'No VM operation required on Linux.')

        status_result = get_vm_status()

        if is_vm_nonexistent(status_result):
            logger.info('Finch VM does not exist. Initializing...')
            result = initialize_vm()
            if result['status'] == 'error':
                return result
            return format_result('success', 'Finch VM was initialized successfully.')
        elif is_vm_stopped(status_result):
            logger.info('Finch VM is stopped. Starting it...')
            result = start_stopped_vm()
            if result['status'] == 'error':
                return result
            return format_result('success', 'Finch VM was started successfully.')
        elif is_vm_running(status_result):
            return format_result('success', 'Finch VM is already running.')
        else:
            return format_result(
                'error',
                f'Unknown VM status: status code {status_result.returncode}',
            )
    except Exception as e:
        return format_result('error', f'Error ensuring Finch VM is running: {str(e)}')


@mcp.tool()
async def finch_build_container_image(request: BuildImageRequest) -> Result:
    """Build a container image using Finch.

    This tool first ensures that the Finch VM is running, starting it if necessary.
    Then it builds a Docker image using the specified Dockerfile and context directory.
    It supports a range of build options including tags, platforms, and more.
    If the Dockerfile contains references to ECR repositories, it verifies that
    ecr login cred helper is properly configured before proceeding with the build.

    Note: for ecr-login to work server needs access to AWS credentials/profile which are configured
    in the server mcp configuration file.

    Arguments:
        request : The request object of type BuildImageRequest containing all build parameters.
            - dockerfile_path (str): Absolute path to the Dockerfile
            - context_path (str): Absolute path to the build context directory
            - tags (List[str], optional): List of tags to apply to the image (e.g., ["myimage:latest", "myimage:v1"])
            - platforms (List[str], optional): List of target platforms (e.g., ["linux/amd64", "linux/arm64"])
            - target (str, optional): Target build stage to build
            - no_cache (bool, optional): Whether to disable cache. Defaults to False.
            - pull (bool, optional): Whether to always pull base images. Defaults to False.
            - add_hosts (List[str], optional): List of custom host-to-IP mappings
            - allow (List[str], optional): List of extra privileged entitlements
            - build_contexts (List[str], optional): List of additional build contexts
            - outputs (str, optional): Output destination
            - cache_from (List[str], optional): List of external cache sources
            - cache_to (List[str], optional): List of cache export destinations
            - quiet (bool, optional): Whether to suppress build output. Defaults to False.
            - progress (str, optional): Type of progress output. Defaults to "auto".

    Returns:
        Result: An object containing:
            - status (str): "success" if the operation succeeded, "error" otherwise
            - message (str): A descriptive message about the result of the operation

    Example response:
        Result(status="success", message="Successfully built image from /path/to/Dockerfile")

    """
    logger.info('tool-name: finch_build_container_image')
    logger.info(
        f'tool-args: dockerfile_path={request.dockerfile_path}, context_path={request.context_path}'
    )

    try:
        finch_install_status = check_finch_installation()
        if finch_install_status['status'] == 'error':
            return Result(**finch_install_status)

        if contains_ecr_reference(request.dockerfile_path):
            logger.info('ECR reference detected in Dockerfile, configuring ECR login')
            config_result = configure_ecr()
            changed = config_result.get('changed', False)
            if changed:
                logger.info('ECR configuration changed, restarting VM')
                stop_vm(force=True)

        vm_status = ensure_vm_running()
        if vm_status['status'] == 'error':
            return Result(**vm_status)

        result = build_image(
            dockerfile_path=request.dockerfile_path,
            context_path=request.context_path,
            tags=request.tags,
            platforms=request.platforms,
            target=request.target,
            no_cache=request.no_cache,
            pull=request.pull,
            add_hosts=request.add_hosts,
            allow=request.allow,
            build_contexts=request.build_contexts,
            outputs=request.outputs,
            cache_from=request.cache_from,
            cache_to=request.cache_to,
            quiet=request.quiet,
            progress=request.progress,
        )
        return Result(**result)
    except Exception as e:
        error_result = format_result('error', f'Error building Docker image: {str(e)}')
        return Result(**error_result)


@mcp.tool()
async def finch_push_image(request: PushImageRequest) -> Result:
    """Push a container image to a repository using finch, replacing the tag with the image hash.

    If the image URL is an ECR repository, it verifies that ECR login cred helper is  configured.
    This tool first ensures that the Finch VM is running, starting it if necessary.
    Then it gets the image hash, creates a new tag using the hash, and pushes the image with
    the hash tag to the repository. If the image URL is an ECR repository, it verifies that
    ECR login is properly configured before proceeding with the push.

    The tool expects the image to be already built and available locally. It uses
    'finch image inspect' to get the hash, 'finch image tag' to create a new tag,
    and 'finch image push' to perform the actual push operation.

    Arguments:
        request (str): The full image name to push, including the repository URL and tag.
                    For ECR repositories, it must follow the format:
                    <aws_account_id>.dkr.ecr.<region>.amazonaws.com/<repository_name>:<tag>

    Returns:
        Result: An object containing:
            - status (str): "success" if the operation succeeded, "error" otherwise
            - message (str): A descriptive message about the result of the operation

    Example response:
        Result(status="success", message="Successfully pushed image 123456789012.dkr.ecr.us-west-2.amazonaws.com/my-repo:latest to ECR.")

    """
    logger.info('tool-name: finch_push_image')
    logger.info(f'tool-args: image={request.image}')

    try:
        finch_install_status = check_finch_installation()
        if finch_install_status['status'] == 'error':
            return Result(**finch_install_status)

        is_ecr = is_ecr_repository(request.image)
        if is_ecr:
            logger.info('ECR repository detected, configuring ECR login')
            config_result = configure_ecr()
            changed = config_result.get('changed', False)
            if changed:
                logger.info('ECR configuration changed, restarting VM')
                stop_vm(force=True)

        vm_status = ensure_vm_running()
        if vm_status['status'] == 'error':
            return Result(**vm_status)

        result = push_image(request.image)
        return Result(**result)
    except Exception as e:
        error_result = format_result('error', f'Error pushing image: {str(e)}')
        return Result(**error_result)


@mcp.tool()
async def finch_create_ecr_repo(request: CreateEcrRepoRequest) -> Result:
    """Check if an ECR repository exists and create it if it doesn't.

    This tool checks if the specified ECR repository exists by using the AWS CLI.
    If the repository doesn't exist, it creates a new one with the given name.
    The tool requires AWS CLI to be installed and configured with appropriate credentials.

    Arguments:
        request: The request object of type CreateEcrRepoRequest containing:
            - app_name (str): The name of the application/repository to check or create in ECR
            - region (str, optional): AWS region for the ECR repository. If not provided, uses the default region
                                     from AWS configuration

    Returns:
        Result: An object containing:
            - status (str): "success" if the operation succeeded, "error" otherwise
            - message (str): A descriptive message about the result of the operation
            - repository_uri (str, optional): The URI of the repository if successful
            - exists (bool, optional): Whether the repository already existed

    Example response:
        Result(status="success", message="Successfully created ECR repository 'my-app'.",
               repository_uri="123456789012.dkr.ecr.us-west-2.amazonaws.com/my-app",
               exists=False)

    """
    logger.info('tool-name: finch_create_ecr_repo')
    logger.info(f'tool-args: app_name={request.app_name}')

    try:
        result = create_ecr_repository(
            app_name=request.app_name,
            region=request.region,
            scan_on_push=True,
            image_tag_mutability='IMMUTABLE',
        )
        return Result(**result)
    except Exception as e:
        error_result = format_result('error', f'Error checking/creating ECR repository: {str(e)}')
        return Result(**error_result)


def main():
    """Run the Finch MCP server."""
    logger.info('Starting Finch MCP server')
    logger.info(f'Logs will be written to: {LOG_FILE}')
    mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
