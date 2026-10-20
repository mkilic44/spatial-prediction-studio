"""Write the Store revision report only from an accepted exact Setup.exe."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    build = read(ROOT / 'installer/dist-claude-store/installer-build-record.json')
    evidence = ROOT / 'installer/final-acceptance-claude-store'
    delivery = read(evidence / 'delivery-acceptance.json')
    acceptance_path = Path(delivery['acceptance_path'])
    acceptance = read(acceptance_path)
    manifest = Path(acceptance['source_payload']) / 'payload-manifest.json'
    revision = read(manifest)['revision']
    check = read(ROOT / 'installer/claude-store-payload-revision.json')
    signature = read(ROOT / 'installer/dist-claude-store/installer-signature-readback.json')
    if not (acceptance['status'] == 'passed' and delivery['status'] == 'passed_isolated_installation'
            and sha(acceptance_path) == delivery['acceptance_sha256']
            and acceptance['installer_sha256'] == build['sha256'] == sha(Path(build['installer']))
            and signature['sha256'] == build['sha256']
            and signature['status'] == 'NotSigned'
            and sha(manifest) == build['manifest_sha256']
            and acceptance['claude_both_profiles_owned_entries_removed']
            and acceptance['claude_unrelated_values_and_bom_preserved']):
        raise ValueError('The Store revision has not passed exact-artifact acceptance')
    suite = ET.parse(ROOT / 'build/claude-profiles-final.xml').getroot().find('testsuite')
    if any(int(suite.attrib.get(name, 0)) for name in ('failures', 'errors', 'skipped')):
        raise ValueError('MCP suite is not fully passed')
    installer_suite = ET.parse(ROOT / 'installer/installer-tests-claude-store.xml').getroot().find('testsuite')
    if any(int(installer_suite.attrib.get(name, 0)) for name in ('failures', 'errors', 'skipped')):
        raise ValueError('Installer unit suite is not fully passed')
    lines = [
        '# Installation acceptance of the Claude Store fix', '',
        f"New content version: `{build['build_id']}`. This file reports only the isolated acceptance of the new installer.", '',
        'On the real computer, the Claude Store app was found to use a different profile. The previous helper wrote only to the classic profile file. The new helper registers both existing supported profiles separately. Other servers are preserved.', '',
        '## File and version binding', '',
        f"- Installer: `{build['installer']}`",
        f"- SHA-256 digest of the installer: `{build['sha256']}`",
        f"- File size: {build['bytes']} bytes.",
        f"- SHA-256 digest of the payload manifest: `{build['manifest_sha256']}`",
        f"- SHA-256 digest of the previous payload manifest: `{revision['parent_manifest_sha256']}`",
        f"- Compiler run time: {delivery['compiler_resource']['elapsed_seconds']:.3f} seconds.",
        f"- Peak total memory of the compiler processes: {delivery['compiler_resource']['peak_owned_tree_rss_MiB']:.3f} MiB.", '',
        f"The file set and SHA-256 digests of the {check['scientific_component_files_identical']} scientific component files are identical to the previous version. This comparison covers the analysis code, the two Python environments, the R environment, the weights and the source add-in package. The compiled code of the native add-in did not change.", '',
        'Changed files:', '',
        *['- `' + item['path'] + '`' for item in revision['changes']], '',
        '## Tested operations', '',
        f"- {suite.attrib['tests']} Model Context Protocol (MCP) tests passed. No errors or skipped tests.",
        f"- The installer tests recorded {installer_suite.attrib['tests']} passing results, including subcases. No errors or skipped cases.",
        '- The seven client options were run in isolated profile directories. A file for manual import was generated for PyCharm.',
        '- The classic Claude and Store Claude profiles were registered in the same installation. The original backup and the new digest of each file were read back.',
        '- In both profiles, other servers, Turkish values and the UTF-8 byte order mark (BOM) were preserved.',
        '- Uninstallation used only the profile paths recorded by the installation. Owned entries were removed; other values were preserved.',
        '- The private Python and R environments, the pinned package versions and all installed file digests were verified.',
        '- The relocated local TabPFN weights were tested with the network disabled: a short training, eight predictions and a model reload.',
        "- The 'caret' Applications for Spatial-Temporal Models (CAST) reference and a complete synthetic Ridge run were executed again. All run outputs were copied to the evidence directory.",
        '- Uninstallation completed while preserving the previous add-in, the user study file and the global PATH setting.', '',
        f"Number of registered application files removed: {acceptance['uninstalled_registered_files']}. The {acceptance['remaining_generated_files']} cache and log files generated by the installation and the tests are counted separately. A separate check verified that the user study file was preserved.", '',
        '## Evidence and scope limits', '',
        # `sps-ç2` names the folder recorded during the Turkish 0.5.0 acceptance; keep as recorded.
        'The new installation was tested in the physical `sps-cs` directory. This directory name contains no Turkish characters. The earlier installation acceptance in the Turkish-named `sps-ç2` directory is separate, historical evidence. The two runs are not combined as if they were the same installation.', '',
        f"Acceptance record: `{acceptance_path}`. SHA-256 digest: `{delivery['acceptance_sha256']}`.", '',
        f"Copy of the complete short Ridge run and the evidence: `{evidence}`. `delivery-acceptance.json` contains the file digests.", '',
        'This acceptance is not a test on a different physical computer. It is not a live connection acceptance with seven separate clients. The real user installation and the client interfaces are shown separately in the main delivery report. The synthetic model test does not count as scientific field validation.', '',
        'The new Setup.exe is not digitally signed. The Windows signature status was read separately from this new file. File integrity must be checked with the SHA-256 digest above.', '',
        'The previous installer and `installer/FINAL_ACCEPTANCE.md` are kept as historical acceptance. Earlier numerical analysis results are not presented as if they had been reproduced.', ''
    ]
    (ROOT / 'installer/CLAUDE_STORE_ACCEPTANCE.md').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
