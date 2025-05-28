"""Tests for the push utility module."""

from awslabs.finch_mcp_server.consts import STATUS_ERROR, STATUS_SUCCESS
from awslabs.finch_mcp_server.utils.push import get_image_hash, is_ecr_repository, push_image
from unittest.mock import MagicMock, patch


class TestIsEcrRepository:
    """Tests for the is_ecr_repository function."""

    def test_valid_ecr_repository(self):
        """Test valid ECR repository URLs."""
        valid_urls = [
            '123456789012.dkr.ecr.us-west-2.amazonaws.com/myrepo:latest',
            '123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo/nested:v1',
            '123456789012.dkr.ecr.eu-central-1.amazonaws.com/repo',
            '123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/my_repo:1.0.0',
        ]

        for url in valid_urls:
            assert is_ecr_repository(url) is True, f'Should identify {url} as ECR repository'

    def test_invalid_ecr_repository(self):
        """Test invalid ECR repository URLs."""
        invalid_urls = [
            'docker.io/library/nginx:latest',
            'quay.io/username/repo:latest',
            'gcr.io/project/image:tag',
            'localhost:5000/myimage:latest',
            'myregistry.example.com/repo:tag',
            '12345.dkr.ecr.us-west-2.amazonaws.com/myrepo:latest',  # Invalid account ID
            '123456789012.ecr.us-west-2.amazonaws.com/myrepo:latest',  # Missing dkr
            '123456789012.dkr.ecr.invalid-region.amazonaws.com/myrepo:latest',  # Invalid region format
        ]

        for url in invalid_urls:
            assert is_ecr_repository(url) is False, f'Should identify {url} as non-ECR repository'


class TestGetImageHash:
    """Tests for the get_image_hash function."""

    @patch('awslabs.finch_mcp_server.utils.push.execute_command')
    @patch('awslabs.finch_mcp_server.utils.push.format_result')
    def test_get_image_hash_success(self, mock_format_result, mock_execute_command):
        """Test successful retrieval of image hash."""
        # Setup mocks
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = (
            '{"Id": "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"}'
        )
        mock_execute_command.return_value = mock_process
        mock_format_result.return_value = {
            'status': STATUS_SUCCESS,
            'message': 'Successfully retrieved hash for image myimage:latest',
            'hash': 'sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef',
        }

        # Call function
        result = get_image_hash('myimage:latest')

        # Verify results
        assert result['status'] == STATUS_SUCCESS
        assert 'Successfully retrieved hash' in result['message']
        assert (
            result['hash']
            == 'sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef'
        )
        mock_execute_command.assert_called_once_with(
            ['finch', 'image', 'inspect', 'myimage:latest']
        )

    @patch('awslabs.finch_mcp_server.utils.push.execute_command')
    @patch('awslabs.finch_mcp_server.utils.push.format_result')
    def test_get_image_hash_command_failure(self, mock_format_result, mock_execute_command):
        """Test handling of command failure when getting image hash."""
        # Setup mocks
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stderr = 'Error: No such image: myimage:latest'
        mock_execute_command.return_value = mock_process
        mock_format_result.return_value = {
            'status': STATUS_ERROR,
            'message': 'Failed to get hash for image myimage:latest: Error: No such image: myimage:latest',
            'stderr': 'Error: No such image: myimage:latest',
        }

        # Call function
        result = get_image_hash('myimage:latest')

        # Verify results
        assert result['status'] == STATUS_ERROR
        assert 'Failed to get hash' in result['message']
        mock_execute_command.assert_called_once_with(
            ['finch', 'image', 'inspect', 'myimage:latest']
        )

    @patch('awslabs.finch_mcp_server.utils.push.execute_command')
    @patch('awslabs.finch_mcp_server.utils.push.format_result')
    def test_get_image_hash_no_hash_found(self, mock_format_result, mock_execute_command):
        """Test handling of missing hash in command output."""
        # Setup mocks
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = '{"Config": {"Labels": {}}}'  # No Id field
        mock_execute_command.return_value = mock_process
        mock_format_result.return_value = {
            'status': STATUS_ERROR,
            'message': 'Could not find hash in image inspect output for myimage:latest',
        }

        # Call function
        result = get_image_hash('myimage:latest')

        # Verify results
        assert result['status'] == STATUS_ERROR
        assert 'Could not find hash' in result['message']
        mock_execute_command.assert_called_once_with(
            ['finch', 'image', 'inspect', 'myimage:latest']
        )


class TestPushImage:
    """Tests for the push_image function."""

    @patch('awslabs.finch_mcp_server.utils.push.get_image_hash')
    @patch('awslabs.finch_mcp_server.utils.push.execute_command')
    @patch('awslabs.finch_mcp_server.utils.push.format_result')
    def test_push_image_success(
        self, mock_format_result, mock_execute_command, mock_get_image_hash
    ):
        """Test successful image push."""
        # Setup mocks
        mock_get_image_hash.return_value = {
            'status': STATUS_SUCCESS,
            'hash': 'sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef',
        }

        mock_tag_process = MagicMock()
        mock_tag_process.returncode = 0

        mock_push_process = MagicMock()
        mock_push_process.returncode = 0
        mock_push_process.stdout = 'Successfully pushed image'

        mock_execute_command.side_effect = [mock_tag_process, mock_push_process]

        mock_format_result.return_value = {
            'status': STATUS_SUCCESS,
            'message': 'Successfully pushed image myrepo:1234567890abcd (original: myrepo:latest).',
            'stdout': 'Successfully pushed image',
        }

        # Call function
        result = push_image('myrepo:latest')

        # Verify results
        assert result['status'] == STATUS_SUCCESS
        assert 'Successfully pushed image' in result['message']

        # Verify command calls
        assert mock_execute_command.call_count == 2
        mock_execute_command.assert_any_call(['finch', 'image', 'push', 'myrepo:1234567890ab'])

    @patch('awslabs.finch_mcp_server.utils.push.get_image_hash')
    def test_push_image_hash_failure(self, mock_get_image_hash):
        """Test handling of failure to get image hash."""
        # Setup mocks
        mock_get_image_hash.return_value = {
            'status': STATUS_ERROR,
            'message': 'Failed to get hash for image myrepo:latest',
        }

        # Call function
        result = push_image('myrepo:latest')

        # Verify results
        assert result['status'] == STATUS_ERROR
        assert 'Failed to get hash' in result['message']

    @patch('awslabs.finch_mcp_server.utils.push.get_image_hash')
    @patch('awslabs.finch_mcp_server.utils.push.execute_command')
    @patch('awslabs.finch_mcp_server.utils.push.format_result')
    def test_push_image_tag_failure(
        self, mock_format_result, mock_execute_command, mock_get_image_hash
    ):
        """Test handling of failure to tag image."""
        # Setup mocks
        mock_get_image_hash.return_value = {
            'status': STATUS_SUCCESS,
            'hash': 'sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef',
        }

        mock_tag_process = MagicMock()
        mock_tag_process.returncode = 1
        mock_tag_process.stderr = 'Error tagging image'

        mock_execute_command.return_value = mock_tag_process

        mock_format_result.return_value = {
            'status': STATUS_ERROR,
            'message': 'Failed to tag image with hash: Error tagging image',
            'stderr': 'Error tagging image',
        }

        # Call function
        result = push_image('myrepo:latest')

        # Verify results
        assert result['status'] == STATUS_ERROR
        assert 'Failed to tag image' in result['message']

    @patch('awslabs.finch_mcp_server.utils.push.get_image_hash')
    @patch('awslabs.finch_mcp_server.utils.push.execute_command')
    @patch('awslabs.finch_mcp_server.utils.push.format_result')
    def test_push_image_push_failure(
        self, mock_format_result, mock_execute_command, mock_get_image_hash
    ):
        """Test handling of failure to push image."""
        # Setup mocks
        mock_get_image_hash.return_value = {
            'status': STATUS_SUCCESS,
            'hash': 'sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef',
        }

        mock_tag_process = MagicMock()
        mock_tag_process.returncode = 0

        mock_push_process = MagicMock()
        mock_push_process.returncode = 1
        mock_push_process.stderr = 'Error pushing image'

        mock_execute_command.side_effect = [mock_tag_process, mock_push_process]

        mock_format_result.return_value = {
            'status': STATUS_ERROR,
            'message': 'Failed to push image myrepo:1234567890abcd: Error pushing image',
            'stderr': 'Error pushing image',
        }

        # Call function
        result = push_image('myrepo:latest')

        # Verify results
        assert result['status'] == STATUS_ERROR
        assert 'Failed to push image' in result['message']

        # Verify command calls
        assert mock_execute_command.call_count == 2
        mock_execute_command.assert_any_call(
            ['finch', 'image', 'tag', 'myrepo:latest', 'myrepo:1234567890ab']
        )
        mock_execute_command.assert_any_call(['finch', 'image', 'push', 'myrepo:1234567890ab'])

    @patch('awslabs.finch_mcp_server.utils.push.get_image_hash')
    @patch('awslabs.finch_mcp_server.utils.push.execute_command')
    @patch('awslabs.finch_mcp_server.utils.push.format_result')
    def test_push_image_without_tag(
        self, mock_format_result, mock_execute_command, mock_get_image_hash
    ):
        """Test pushing an image without a tag."""
        # Setup mocks
        mock_get_image_hash.return_value = {
            'status': STATUS_SUCCESS,
            'hash': 'sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef',
        }

        mock_tag_process = MagicMock()
        mock_tag_process.returncode = 0

        mock_push_process = MagicMock()
        mock_push_process.returncode = 0
        mock_push_process.stdout = 'Successfully pushed image'

        mock_execute_command.side_effect = [mock_tag_process, mock_push_process]

        mock_format_result.return_value = {
            'status': STATUS_SUCCESS,
            'message': 'Successfully pushed image myrepo:1234567890ab (original: myrepo).',
            'stdout': 'Successfully pushed image',
        }

        # Call function
        result = push_image('myrepo')

        # Verify results
        assert result['status'] == STATUS_SUCCESS
        assert 'Successfully pushed image' in result['message']

        # Verify command calls
        assert mock_execute_command.call_count == 2
        mock_execute_command.assert_any_call(
            ['finch', 'image', 'tag', 'myrepo', 'myrepo:1234567890ab']
        )
        mock_execute_command.assert_any_call(['finch', 'image', 'push', 'myrepo:1234567890ab'])
