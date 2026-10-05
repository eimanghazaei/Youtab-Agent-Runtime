"""Bedrock setup must prepare its optional SDK before regional model selection."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize('entrypoint', ['setup', 'inventory'])
def test_fresh_bedrock_bootstraps_sdk_before_eu_catalog(tmp_path, entrypoint):
    script = r'''
import sys
import os
import types
from unittest.mock import patch
from youtab_agent_cli import auth, config, model_setup_flows, model_switch, models
from tools import lazy_deps
from agent import models_dev

assert 'agent.bedrock_adapter' not in sys.modules
sys.modules.pop('boto3', None)
events = []

class Control:
    def list_foundation_models(self):
        events.append('catalog')
        return {'modelSummaries': []}
    def list_inference_profiles(self, **kwargs):
        return {'inferenceProfileSummaries': [{
            'inferenceProfileId': 'eu.anthropic.test-model',
            'inferenceProfileName': 'EU synthetic model', 'status': 'ACTIVE',
        }]}

def prepare(feature, **kwargs):
    assert feature == 'provider.bedrock'
    events.append('bootstrap')
    sdk = types.ModuleType('boto3')
    sdk.__version__ = '1.34.59'
    def client(service, region_name):
        assert service == 'bedrock' and region_name == 'eu-central-1'
        return Control()
    sdk.client = client
    sys.modules['boto3'] = sdk
    return True

def choose(ids, **kwargs):
    assert ids == ['eu.anthropic.test-model']
    events.append('selection')
    return None

with patch.object(lazy_deps, 'ensure', prepare), \
     patch.object(auth, '_prompt_model_selection', choose), \
     patch.object(config, 'load_config', lambda: {}), \
     patch('builtins.input', lambda *args: ''):
    if os.environ['SYNTHETIC_ENTRYPOINT'] == 'setup':
        model_setup_flows._model_flow_bedrock({})
        assert events == ['bootstrap', 'catalog', 'selection'], events
    else:
        with patch.object(models_dev, 'fetch_models_dev', lambda: {}), \
             patch.object(models, 'get_curated_youtab_model_ids', lambda: []):
            inventory = model_switch.list_authenticated_providers(current_provider='bedrock')
        bedrock = next(p for p in inventory if p['slug'] == 'bedrock')
        assert bedrock['models'] == ['eu.anthropic.test-model'], bedrock
        assert events[0] == 'bootstrap' and 'catalog' in events, events
'''
    env = {**os.environ, 'YOUTAB_AGENT_HOME': str(tmp_path),
           'AWS_REGION': 'eu-central-1', 'AWS_ACCESS_KEY_ID': 'synthetic',
           'AWS_SECRET_ACCESS_KEY': 'synthetic', 'AWS_EC2_METADATA_DISABLED': 'true',
           'SYNTHETIC_ENTRYPOINT': entrypoint}
    result = subprocess.run([sys.executable, '-c', script],
                            cwd=Path(__file__).resolve().parents[2], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
