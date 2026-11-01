"""Prevent a successful checkout or an incomplete run from passing acceptance."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('acceptance', Path(__file__).parents[1] / 'scripts/stage11-accept.py')
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


@pytest.mark.parametrize('result,finished,expected_running', [
    ('FAILURE', 'FAILURE', False), ('SUCCESS', 'SUCCESS', False),
    (None, None, True), ('SUCCESS', None, True),
])
def test_direct_result_and_terminal_marker_are_both_required(tmp_path, monkeypatch, result, finished, expected_running):
    build = tmp_path / '6'
    build.mkdir()
    root_result = '<result>' + result + '</result>' if result else ''
    (build / 'build.xml').write_text('<flow-build><actions><checkout><result>SUCCESS</result></checkout></actions>' + root_result + '</flow-build>')
    (build / 'log').write_text('Finished: ' + finished + '\n' if finished else 'Running')
    monkeypatch.setattr(acceptance, 'BASE', tmp_path)
    report = acceptance.collect_build(build)
    assert report['result'] == result
    assert report['in_progress'] is expected_running


def test_pipeline_trigger_property_is_found(tmp_path, monkeypatch):
    (tmp_path / 'builds').mkdir()
    (tmp_path / 'config.xml').write_text('''<flow-definition><properties>
<org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty><triggers>
<hudson.triggers.SCMTrigger><spec>H/2 * * * *</spec></hudson.triggers.SCMTrigger>
</triggers></org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>
</properties><definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition"/></flow-definition>''')
    monkeypatch.setattr(acceptance, 'JOB', tmp_path)
    monkeypatch.setattr(acceptance, 'command', lambda args: (1, ''))
    report = acceptance.collect()
    assert report['job']['poll_specs'] == ['H/2 * * * *']
    assert report['job']['trigger_types'] == ['hudson.triggers.SCMTrigger']


def test_secret_scan_exports_counts_only():
    result = acceptance.scan('-----BEGIN OPENSSH PRIVATE KEY-----\npassword=never-export-this\n+ env\n')
    assert result == {'private_key_hits': 1, 'password_assignment_hits': 1, 'environment_dump_hits': 1}
    assert 'never-export-this' not in str(result)
