"""Evidence I/O adversarial tests using temporary synthetic files only."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from tools import campaign_rework_evidence as evidence

class EvidenceReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        (self.root/'nested').mkdir()
        self.file=self.root/'nested/receipt.json'
        self.data=b'{"status":"failed","exit_code":-1}\n'
        self.file.write_bytes(self.data)
        self.sha=hashlib.sha256(self.data).hexdigest()
    def read(self,relative='nested/receipt.json',**kw):
        return evidence.read_regular(self.root,relative,self.sha,**kw)
    def test_original_and_strict_json(self):
        self.assertEqual(self.read(),self.data)
        self.assertEqual(evidence.unique_json(self.read())['exit_code'],-1)
        with self.assertRaises(ValueError):evidence.unique_json(b'{"a":1,"a":2}')
        for value in ('NaN','Infinity','-Infinity'):
            with self.assertRaises(ValueError):evidence.unique_json('{"value":'+value+'}')
    def test_unbounded_paths_hashes_and_sizes(self):
        for path in ('','/etc/passwd','../outside','nested/../nested/receipt.json','nested//receipt.json','./nested/receipt.json'):
            with self.subTest(path=path),self.assertRaises(ValueError):self.read(path)
        for value in (None,True,0,'bad','0'*64):
            with self.subTest(hash=value),self.assertRaises(ValueError):evidence.read_regular(self.root,'nested/receipt.json',value)
        for limit in (0,-1,True,129*1024*1024,1):
            with self.subTest(limit=limit),self.assertRaises(ValueError):self.read(limit=limit)
    def test_nonregular_and_symlink_references(self):
        (self.root/'leaf').symlink_to(self.file)
        (self.root/'linked').symlink_to(self.root/'nested',target_is_directory=True)
        os.mkfifo(self.root/'fifo')
        for relative in ('leaf','linked/receipt.json','fifo','nested'):
            with self.subTest(relative=relative),self.assertRaises((OSError,ValueError)):self.read(relative)
        with self.assertRaises(ValueError):evidence.read_regular(self.root/'linked','receipt.json',self.sha)
    def test_content_changes_during_read(self):
        original=os.read
        changed=False
        def read(fd,size):
            nonlocal changed
            data=original(fd,size)
            if not changed:
                changed=True
                self.file.write_bytes(self.data+b'changed')
            return data
        with mock.patch.object(evidence.os,'read',read),self.assertRaises(ValueError):self.read()
    def test_ancestor_swapped_after_canonicalization(self):
        original=Path.resolve
        raced=False
        target=self.root/'nested'
        def resolve(path,*args,**kwargs):
            nonlocal raced
            result=original(path,*args,**kwargs)
            if path==target and not raced:
                raced=True
                target.rename(self.root/'moved')
                target.symlink_to(self.root/'moved',target_is_directory=True)
            return result
        with mock.patch.object(Path,'resolve',resolve),self.assertRaises(OSError):
            evidence.read_regular(target,'receipt.json',self.sha)

if __name__=='__main__':unittest.main()
