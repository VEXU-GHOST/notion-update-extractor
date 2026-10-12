import unittest
from unittest.mock import Mock
import notion_feed as n

DB='11111111-1111-4111-8111-111111111111'
DS='22222222-2222-4222-8222-222222222222'
SRC='33333333-3333-4333-8333-333333333333'
PAGE='44444444-4444-4444-8444-444444444444'
REF='55555555-5555-4555-8555-555555555555'
CHILD='66666666-6666-4666-8666-666666666666'
class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.api=n.API('fake')
        self.api.write_database=DB
        self.api.write_source=DS
        self.api.writable_ids.add(PAGE)
    def test_sources_are_read_only(self):
        self.api.call=Mock(return_value={'type':'paragraph','parent':{'type':'data_source_id','data_source_id':SRC}})
        for method,path,body in [('PATCH','pages/'+SRC,{}),('DELETE','blocks/'+SRC,None),('PATCH','blocks/'+SRC+'/children',{'children':[]}),('POST','pages',{'parent':{'type':'data_source_id','data_source_id':SRC}}),('PATCH','data_sources/'+SRC,{'properties':{}}),('POST','databases',{})]:
            with self.subTest(path=path), self.assertRaises(ValueError): self.api.check_write(method,path,body)
    def test_synced_reference_and_descendants_cannot_be_modified(self):
        def read(method,path):
            if path.endswith(REF): return {'type':'synced_block','parent':{'type':'page_id','page_id':PAGE}}
            return {'type':'paragraph','parent':{'type':'block_id','block_id':REF}}
        self.api.call=read
        for ident in (REF,CHILD):
            with self.assertRaises(ValueError): self.api.check_write('PATCH','blocks/'+ident+'/children',{})
    def test_updates_allowed_and_queries_read_only(self):
        self.api.check_write('POST','pages',{'parent':{'type':'data_source_id','data_source_id':DS}})
        self.api.check_write('PATCH','blocks/'+PAGE+'/children',{'children':[]})
        self.api.check_write('PATCH','data_sources/'+DS,{'properties':{}})
        self.api.check_write('POST','data_sources/'+SRC+'/query',{})
    def test_unscoped_upload_and_move_blocked(self):
        with self.assertRaises(ValueError): self.api.check_write('POST','file_uploads',{'mode':'external_url'})
        with self.assertRaises(ValueError): self.api.import_for_summary(SRC,'https://example.com/a.png','a.png')
        with self.assertRaises(ValueError): self.api.check_write('PATCH','pages/'+PAGE,{'parent':{'page_id':SRC}})
    def test_reciprocal_relation_writes_blocked(self):
        with self.assertRaises(ValueError):
            self.api.check_write('PATCH','pages/'+PAGE,{'properties':{'Task':{'relation':[{'id':SRC}]}}})

    def test_auth_redirects_are_blocked(self):
        with self.assertRaises(RuntimeError):
            n.NoAuthRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example')

    def test_authorize_rejects_source_and_wrong_destination(self):
        with self.assertRaises(ValueError): self.api.authorize_updates(DB,SRC,(SRC,))
        self.api.call=Mock(return_value={'data_sources':[{'id':SRC}]})
        with self.assertRaises(ValueError): self.api.authorize_updates(DB,DS,(SRC,))
if __name__=='__main__': unittest.main()
