"""Unit tests for the read-only SPL guardrail. Offline — no Splunk required.

    py -m pytest tests/test_spl_guard.py
"""

import pytest

from triage.spl_guard import SplGuardError, check_spl


class TestCleanSplPasses:
    def test_plain_search(self):
        spl = 'index=triage_demo sourcetype=payments:api status=error'
        assert check_spl(spl) == spl

    def test_stats_pipeline(self):
        spl = 'index=triage_demo | stats count by error_code | sort -count'
        assert check_spl(spl) == spl

    def test_generating_command(self):
        spl = '| tstats count where index=triage_demo by sourcetype'
        assert check_spl(spl) == spl

    def test_forbidden_word_as_field_value_is_allowed(self):
        # "delete" appearing in log text / field values is not a command
        spl = 'index=triage_demo message="user requested account delete"'
        assert check_spl(spl) == spl


class TestForbiddenCommandsBlocked:
    @pytest.mark.parametrize("spl", [
        'index=triage_demo | delete',
        'index=a | stats count | outputlookup evil.csv',
        'index=a | collect index=other',
        'index=a | sendemail to="x@example.com"',
        '| script python badthing',
        'index=a | eval x=1 | outputcsv dump.csv',
    ])
    def test_blocked(self, spl):
        with pytest.raises(SplGuardError):
            check_spl(spl)

    def test_case_insensitive(self):
        with pytest.raises(SplGuardError):
            check_spl('index=a | DELETE')

    def test_whitespace_around_pipe(self):
        with pytest.raises(SplGuardError):
            check_spl('index=a |   outputlookup   x.csv')


class TestEdgeCases:
    def test_empty_spl_rejected(self):
        with pytest.raises(SplGuardError):
            check_spl("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(SplGuardError):
            check_spl("   ")

    def test_error_names_offending_command(self):
        with pytest.raises(SplGuardError, match="outputlookup"):
            check_spl('index=a | outputlookup x.csv')
