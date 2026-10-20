"""Read-only, no-model startup diagnostic. Never emits raw host diagnostics."""
import importlib.util
import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHRASES = ['unknown feature', 'unrecognized', 'invalid', 'failed', 'denied', 'config',
           'locked', 'profile', 'error', 'permission', 'not found', 'unsupported',
           'experimental', 'app-server', 'plugins', 'remote_plugin', 'skill_mcp',
           'apps', 'thread', 'initialize', 'code-mode', 'code mode', 'must be',
           'cannot', 'sandbox', 'stdio', 'keychain', 'credential', 'trust', 'cwd',
           'null', 'panic', 'decode', 'parse', 'boolean']
TRACE = []
spec = importlib.util.spec_from_file_location('host_probe', HERE / 'mcp-codex-host-probe.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DiagnosticRpc(mod.Rpc):
    def __init__(self, *args, **kwargs):
        self.trace = {'calls': [], 'rpc_errors': []}
        TRACE.append(self.trace)
        super().__init__(*args, **kwargs)

    def request(self, method, params):
        entry = {'method': method, 'status': 'started'}
        self.trace['calls'].append(entry)
        try:
            value = super().request(method, params)
            entry['status'] = 'passed'
            return value
        except mod.ProbeError as error:
            entry['status'] = error.code
            raise

    def _put(self, value):
        if isinstance(value, dict) and isinstance(value.get('error'), dict):
            raw = str(value['error'].get('message', '')).lower()
            self.trace['rpc_errors'].append({'code': value['error'].get('code'),
                'categories': [word for word in PHRASES if word in raw]})
        super()._put(value)

    def _stderr(self):
        self.diagnostic = bytearray()
        while True:
            raw = self.process.stderr.read(4096)
            if not raw:
                return
            if len(self.diagnostic) + len(raw) <= 65536:
                self.diagnostic.extend(raw)

    def close(self):
        super().close()
        raw = bytes(getattr(self, 'diagnostic', b'')).decode('utf-8', 'replace').lower()
        self.trace.update({'exit_code': self.process.returncode,
            'diagnostic_categories': [word for word in PHRASES if word in raw],
            'diagnostic_bytes': len(raw.encode('utf-8')),
            'diagnostic_line_count': len(raw.splitlines()),
            'windows_error_codes': re.findall(r'\b(?:os error|winerror) (\d+)\b', raw)})


install = Path(r'C:\Users\MIRAC\AppData\Local\Programs\SpatialPredictionStudio\versions\0.5.0-70573b90e438')
codex = shutil.which('codex.exe')
result = {'codex_executable': codex, 'scope': 'read_only_host_probe', 'raw_diagnostics_saved': False}
try:
    result['result'] = mod.run_probe(codex, install, '46af696a382d48cc8d2a2bba50f56d4d', rpc_factory=DiagnosticRpc)
    result['passed'] = True
except mod.ProbeError as error:
    result.update({'passed': False, 'error_code': error.code})
result['trace'] = TRACE
path = HERE / 'mcp-codex-startup-diagnostic.json'
path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result))
