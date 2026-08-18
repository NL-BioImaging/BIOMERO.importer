import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

# Get the absolute path to the test_config directory
TEST_CONFIG_DIR = Path(__file__).parent / 'test_config'

def mock_load_settings(file_path):
    if file_path == "config/settings.yml":
        return {
            'base_dir': str(TEST_CONFIG_DIR),
            'group_list': str(TEST_CONFIG_DIR / 'sample_groups_list.json'),
            'upload_orders_dir_name': 'upload_orders',
            'completed_orders_dir_name': 'completed_orders',
            'failed_uploads_directory_name': 'failed_uploads',
            'log_file_path': str(TEST_CONFIG_DIR / 'test.log'),
            'max_workers': 4
        }
    elif file_path.endswith('sample_groups_list.json'):
        with open(TEST_CONFIG_DIR / 'sample_groups_list.json', 'r') as f:
            return json.load(f)
    else:
        raise ValueError(f"Unexpected file path in mock: {file_path}")

@pytest.fixture
def mock_config():
    return mock_load_settings("config/settings.yml")

@pytest.fixture
def mock_groups_info():
    return mock_load_settings("sample_groups_list.json")

@pytest.fixture(autouse=True)
def mock_main_imports():
    # Only mock what's actually needed - some tests may not need any mocks
    yield

@pytest.fixture
def mock_order_info():
    return {
        'Group': 'Private',
        'Username': 'TestUser',
        'Dataset': 'TestDataset',
        'UUID': 'TestUUID',
        'Files': ['file1.txt', 'file2.txt'],
        'file_names': ['file1.txt', 'file2.txt'],
        'UserID': 1,
        'GroupID': 1,
        'ProjectID': 1,
        'DatasetID': 1,
        'ScreenID': None
    }

def test_data_package_initialization():
    """Test DataPackage class initialization."""
    from biomero_importer.main import DataPackage
    order_info = {
        'Group': 'Private',
        'Username': 'TestUser',
        'Dataset': 'TestDataset',
        'UUID': 'TestUUID',
        'Files': ['file1.txt', 'file2.txt'],
        'FileNames': ['file1.txt', 'file2.txt'],
        'UserID': 1,
        'GroupID': 1,
        'ProjectID': 1,
        'DatasetID': 1,
        'ScreenID': None
    }
    data_package = DataPackage(order_info, str(TEST_CONFIG_DIR))
    
    # Test the actual interface - DataPackage uses .get() method, not direct attribute access
    assert data_package.get('Group') == order_info['Group']
    assert data_package.get('Username') == order_info['Username']
    assert data_package.get('Dataset') == order_info['Dataset']
    assert data_package.get('UUID') == order_info['UUID']
    assert data_package.get('Files') == order_info['Files']
    assert data_package.get('FileNames') == order_info['FileNames']
    assert data_package.get('UserID') == order_info['UserID']
    assert data_package.get('GroupID') == order_info['GroupID']
    assert data_package.get('ProjectID') == order_info['ProjectID']
    assert data_package.get('DatasetID') == order_info['DatasetID']
    assert data_package.get('ScreenID') == order_info['ScreenID']

def test_data_package_str_representation(mock_order_info):
    """Test DataPackage string representation."""
    from biomero_importer.main import DataPackage
    data_package = DataPackage(mock_order_info, str(TEST_CONFIG_DIR))
    str_repr = str(data_package)
    # DataPackage inherits from UserDict, so str() shows the dict contents
    assert "'Group': 'Private'" in str_repr
    assert "'Username': 'TestUser'" in str_repr
    assert "'Dataset': 'TestDataset'" in str_repr
    assert "'UUID': 'TestUUID'" in str_repr
    assert "'Files': ['file1.txt', 'file2.txt']" in str_repr


def test_data_package_get_method(mock_order_info):
    """Test DataPackage get method."""
    from biomero_importer.main import DataPackage
    data_package = DataPackage(mock_order_info, str(TEST_CONFIG_DIR))
    assert data_package.get('Group') == 'Private'
    assert data_package.get('NonExistentKey', 'DefaultValue') == 'DefaultValue'


def test_load_config():
    """Test load_config function."""
    from biomero_importer.main import load_config
    
    # Create a mock YAML config file path
    import tempfile
    import yaml
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml',
                                     delete=False) as f:
        yaml.dump({'test_key': 'test_value'}, f)
        temp_config_path = f.name
    
    try:
        config = load_config(temp_config_path)
        assert config['test_key'] == 'test_value'
    finally:
        import os
        os.unlink(temp_config_path)


def test_create_executor(mock_config):
    with patch('biomero_importer.main.ProcessPoolExecutor') as mock_executor:
        from biomero_importer.main import create_executor, initialize_worker
        create_executor(mock_config)
        mock_executor.assert_called_once_with(
            max_workers=mock_config.get('max_workers', 4),
            initializer=initialize_worker,
            initargs=(mock_config,),
        )


def test_initialize_worker_prepares_process_local_tracker(mock_config):
    with patch('biomero_importer.main.logging.basicConfig'), \
         patch('biomero_importer.main.logging.FileHandler'), \
         patch('biomero_importer.main.logging.StreamHandler'), \
         patch('biomero_importer.main.prepare_ingest_tracker_for_worker') as prepare:
        from biomero_importer.main import initialize_worker

        initialize_worker(mock_config)

        prepare.assert_called_once_with(mock_config)


@pytest.fixture
def mock_database_poller():
    with patch('biomero_importer.main.DatabasePoller') as mock_dp:
        yield mock_dp.return_value


def test_run_application(mock_config, mock_groups_info, mock_database_poller):
    mock_executor = MagicMock()
    
    with patch('biomero_importer.main.signal.signal') as mock_signal, \
         patch('biomero_importer.main.time.sleep',
               side_effect=[None, Exception("Stop loop")]):
        
        from biomero_importer.main import run_application
        
        try:
            run_application(mock_config, mock_groups_info,
                            mock_executor)
        except Exception as e:
            # Ensure we exited due to our forced exception
            assert str(e) == "Stop loop"
        
        # Should be called twice for SIGINT and SIGTERM
        assert mock_signal.call_count == 2
        mock_database_poller.start.assert_called_once()
        mock_database_poller.stop.assert_called_once()
        mock_executor.shutdown.assert_called_once_with(wait=True)
