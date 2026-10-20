"""Isolated filesystem transaction tests; no real AddIns or registry writes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import zipfile

spec = importlib.util.spec_from_file_location('sps_deploy', Path(__file__).with_name('deploy.py'))
deploy = importlib.util.module_from_spec(spec); spec.loader.exec_module(deploy)

class DeploymentTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / 'test-work'; scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch); self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'application'; self.root.mkdir(); self.docs = self.base / 'Documents'
        self.target = self.docs / 'ArcGIS/AddIns/Desktop10.8' / deploy.GUID / 'SpatialPredictionStudio.esriAddIn'
        package = self.root / 'package/SpatialPredictionStudio.esriAddIn'; package.parent.mkdir()
        members = {'Config.xml': b'<fixture/>', 'Install/runtime.json': b'{"python_executable":"old-machine"}', 'Install/backend/worker.py': b'fixture'}
        record = {'version':'0.4.1','sources':{'fixture':'unchanged'},'members':{n:hashlib.sha256(v).hexdigest() for n,v in members.items()},'external_assets':{}}
        with zipfile.ZipFile(package,'w') as z:
            for n,v in members.items():z.writestr(n,v)
            z.writestr('Install/build-manifest.json',json.dumps(record))
        self.manifest = {'schema_version':1,'product':'SpatialPredictionStudio','components':['core'],'source_addin_sha256':deploy.sha(package),'files':{'package/SpatialPredictionStudio.esriAddIn':{'sha256':deploy.sha(package),'component':'core'}}}
        deploy.atomic_json(self.root/'payload-manifest.json',self.manifest)

    def tearDown(self):self.temp.cleanup()

    def activate(self,**kwargs): return deploy.activate(self.root,['core'],self.docs,staging=True,run_probe=False,**kwargs)

    def test_verify_only_performs_no_writes(self):
        before={str(p):deploy.sha(p) for p in self.base.rglob('*') if p.is_file()}
        _,n=deploy.verify_payload(self.root,['core']);self.assertEqual(n,1)
        self.assertEqual(before,{str(p):deploy.sha(p) for p in self.base.rglob('*') if p.is_file()});self.assertFalse(self.docs.exists())

    def test_manifest_hash_is_anchored(self):
        with self.assertRaises(ValueError):deploy.verify_payload(self.root,['core'],'0'*64)

    def test_changed_payload_blocks_activation_without_addin(self):
        (self.root/'package/SpatialPredictionStudio.esriAddIn').write_bytes(b'tampered')
        with self.assertRaises(ValueError):self.activate()
        self.assertFalse(self.target.exists())

    def test_manifest_path_cannot_escape_owned_directory(self):
        for value in ('../foreign.txt','C:/foreign.txt','/foreign.txt','a\\..\\foreign.txt'):
            with self.subTest(value=value),self.assertRaises(ValueError):deploy.inside(self.root,value)

    def test_unknown_component_is_rejected(self):
        with self.assertRaises(ValueError):deploy.verify_payload(self.root,['core','unknown'])

    def test_core_is_required(self):
        with self.assertRaises(ValueError):deploy.verify_payload(self.root,[])

    def test_activation_rewrites_only_runtime_and_manifest(self):
        result=self.activate();self.assertEqual(result['status'],'installed')
        self.assertEqual(deploy.sha(self.target),result['addin_sha256'])
        with zipfile.ZipFile(self.target) as z:
            runtime=json.loads(z.read('Install/runtime.json'))
            self.assertEqual(runtime['python_executable'],str(self.root/'runtime-d/Scripts/python.exe'))
            self.assertEqual(z.read('Install/backend/worker.py'),b'fixture')
            manifest=json.loads(z.read('Install/build-manifest.json'))
            self.assertEqual(manifest['sources'],{'fixture':'unchanged'})
            for name,value in manifest['members'].items():self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(),value)

    def test_existing_addin_is_backed_up_and_restored_on_uninstall(self):
        self.target.parent.mkdir(parents=True);self.target.write_bytes(b'previous user package')
        self.activate();self.assertEqual((self.root/'activation-backup.esriAddIn').read_bytes(),b'previous user package')
        result=deploy.uninstall(self.root,self.docs,staging=True)
        self.assertEqual(result['status'],'unregistered_owned_addin');self.assertEqual(self.target.read_bytes(),b'previous user package')

    def test_failure_rolls_back_existing_addin(self):
        self.target.parent.mkdir(parents=True);self.target.write_bytes(b'previous')
        with self.assertRaisesRegex(RuntimeError,'Injected'):self.activate(fail_after_activation=True)
        self.assertEqual(self.target.read_bytes(),b'previous');self.assertFalse((self.root/'deployment-record.json').exists())

    def test_failure_removes_only_newly_created_addin(self):
        with self.assertRaisesRegex(RuntimeError,'Injected'):self.activate(fail_after_activation=True)
        self.assertFalse(self.target.exists());self.assertEqual(list(self.target.parent.glob('*.new')),[])

    def test_modified_addin_survives_uninstall(self):
        self.activate();self.target.write_bytes(b'changed after install')
        result=deploy.uninstall(self.root,self.docs,staging=True)
        self.assertEqual(result['status'],'preserved_modified_addin');self.assertEqual(self.target.read_bytes(),b'changed after install')

    def test_installed_file_readback_detects_later_changes(self):
        self.activate();self.assertEqual(deploy.verify_installed(self.root)['status'],'installed_files_verified')
        (self.root/'package/SpatialPredictionStudio.esriAddIn').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'Installed file changed'):deploy.verify_installed(self.root)

    def test_user_results_and_foreign_files_survive(self):
        self.activate();foreign=self.base/'user-result.tif';foreign.write_bytes(b'user data')
        deploy.uninstall(self.root,self.docs,staging=True)
        self.assertEqual(foreign.read_bytes(),b'user data');self.assertFalse(self.target.exists())

    def test_forged_uninstall_target_is_rejected(self):
        self.activate();record=deploy.read(self.root/'deployment-record.json');record['addin_path']=str(self.base/'foreign')
        deploy.atomic_json(self.root/'deployment-record.json',record)
        with self.assertRaises(ValueError):deploy.uninstall(self.root,self.docs,staging=True)
        self.assertTrue(self.target.exists())

    def test_modified_backup_cannot_replace_active_addin(self):
        self.target.parent.mkdir(parents=True);self.target.write_bytes(b'old');self.activate();digest=deploy.sha(self.target)
        (self.root/'activation-backup.esriAddIn').write_bytes(b'bad backup')
        with self.assertRaises(ValueError):deploy.uninstall(self.root,self.docs,staging=True)
        self.assertEqual(deploy.sha(self.target),digest)

    def test_repeat_activation_cannot_overwrite_ownership_record(self):
        self.activate()
        with self.assertRaises(FileExistsError):self.activate()

    def test_uninstall_does_not_restore_removed_previous_runtime(self):
        self.target.parent.mkdir(parents=True)
        with zipfile.ZipFile(self.target,'w') as z:
            z.writestr('Install/build-manifest.json',json.dumps({'installation':{'relocated':True}}))
            z.writestr('Install/runtime.json',json.dumps({'python_executable':str(self.base/'removed/python.exe'),'worker_path':str(self.base/'removed/worker.py')}))
        self.activate()
        result=deploy.uninstall(self.root,self.docs,staging=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(result['previous_addin_restoration'],'skipped_previous_runtime_missing')

    def test_unlisted_addin_member_blocks_activation(self):
        package=self.root/'package/SpatialPredictionStudio.esriAddIn'
        with zipfile.ZipFile(package,'a') as z:z.writestr('Install/unlisted.dll',b'unexpected')
        self.manifest['files']['package/SpatialPredictionStudio.esriAddIn']['sha256']=deploy.sha(package)
        deploy.atomic_json(self.root/'payload-manifest.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'manifest member set'):self.activate()
        self.assertFalse(self.target.exists())

    def test_unsafe_addin_member_blocks_activation(self):
        package=self.root/'package/SpatialPredictionStudio.esriAddIn'
        with zipfile.ZipFile(package,'a') as z:z.writestr('../foreign.dll',b'unexpected')
        self.manifest['files']['package/SpatialPredictionStudio.esriAddIn']['sha256']=deploy.sha(package)
        deploy.atomic_json(self.root/'payload-manifest.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'Unsafe manifest path'):self.activate()
        self.assertFalse(self.target.exists())

    def test_mcp_default_generates_templates_without_application(self):
        with patch.object(deploy.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"clients":[]}',stderr='')) as run:
            deploy.configure_clients(self.root,[],staging_root=self.base)
        args=run.call_args.args[0]
        self.assertNotIn('--apply',args);self.assertNotIn('--remove',args)
        self.assertEqual(run.call_args.kwargs['env']['USERPROFILE'],str(self.base/'ClientHome'))
        self.assertEqual(run.call_args.kwargs['env']['LOCALAPPDATA'],str(self.base/'ClientHome/AppData/Local'))
        self.assertTrue(Path(run.call_args.kwargs['env']['CODEX_HOME']).is_relative_to(self.base))

    def test_example_paths_relocate_without_changing_data_or_settings(self):
        folder=self.root/'examples-d';folder.mkdir()
        data=folder/'samples.csv';data.write_text('id,y\n1,7\n')
        original={'samples':{'path':str(self.base/'builder/examples-d/samples.csv'),'unit':'g/kg'},'notes':'Türkçe ıİğĞşŞ','seed':42}
        path=folder/'test.request.json';deploy.atomic_json(path,original)
        changed=deploy.relocate_examples(self.root,str(self.base/'builder'))
        readback=deploy.read(path)
        self.assertEqual(readback['samples']['path'],str(data));self.assertEqual(readback['notes'],original['notes'])
        self.assertEqual(readback['seed'],42);self.assertEqual(data.read_text(),'id,y\n1,7\n');self.assertEqual(len(changed),1)

    def test_mcp_only_selected_clients_are_applied_and_removed(self):
        with patch.object(deploy.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"clients":[]}',stderr='')) as run:
            deploy.configure_clients(self.root,['codex','pycharm'],staging_root=self.base)
            first=run.call_args.args[0]
            deploy.configure_clients(self.root,['codex'],staging_root=self.base,remove=True)
            last=run.call_args.args[0]
        self.assertIn('--apply',first);self.assertEqual(first.count('--client'),2)
        self.assertIn('--remove',last);self.assertNotIn('--apply',last)
        self.assertIn('--registration-record',last)
        self.assertEqual(last[last.index('--registration-record')+1],str(self.root/'client-configurations/registration-result.json'))
        self.assertEqual(last[-2:],['--client','codex'])

    def test_failed_owned_client_removal_raises_before_application_removal(self):
        with patch.object(deploy.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='{"clients":[{"status":"not_registered"}]}',stderr='')):
            with self.assertRaisesRegex(RuntimeError,'application files are retained'):
                deploy.configure_clients(self.root,['claude_desktop'],staging_root=self.base,remove=True)

if __name__=='__main__':unittest.main(verbosity=2)
