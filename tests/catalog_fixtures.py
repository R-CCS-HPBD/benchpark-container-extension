# SPDX-License-Identifier: Apache-2.0
"""Real filesystem blobs for OCI validation; not a container execution claim."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
from benchpark_container.util import json_bytes


def oci_layout(path, platforms=('linux/amd64',), nested=False):
    path = Path(path); (path/'blobs/sha256').mkdir(parents=True)
    blobdir = path/'blobs/sha256'
    def blob(value, media):
        raw = value if isinstance(value, bytes) else json_bytes(value)
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        (blobdir/digest[7:]).write_bytes(raw)
        return {'digest':digest,'size':len(raw),'mediaType':media}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        info=tarfile.TarInfo('fixture.txt'); content=b'OCI STORE FIXTURE - NOT EXECUTED\n'
        info.size=len(content);info.mtime=0;tar.addfile(info,io.BytesIO(content))
    layer = blob(gzip.compress(stream.getvalue(),mtime=0),'application/vnd.oci.image.layer.v1.tar+gzip')
    diffid = 'sha256:'+hashlib.sha256(stream.getvalue()).hexdigest()
    manifests=[]
    for platform in platforms:
        os_, arch=platform.split('/')[:2]
        config={'architecture':arch,'os':os_,'config':{'Env':[]},'rootfs':{'type':'layers','diff_ids':[diffid]}}
        c=blob(config,'application/vnd.oci.image.config.v1+json')
        doc={'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json','config':c,'layers':[layer]}
        m=blob(doc,doc['mediaType']);m['platform']={'os':os_,'architecture':arch};manifests.append(m)
    if len(manifests)>1 or nested:
        doc={'schemaVersion':2,'mediaType':'application/vnd.oci.image.index.v1+json','manifests':manifests}
        top=blob(doc,doc['mediaType'])
    else:top=manifests[0]
    (path/'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    (path/'index.json').write_bytes(json_bytes({'schemaVersion':2,'manifests':[top]}))
    return {'root':path,'digest':top['digest'],'layer':layer,'manifests':manifests}


def sif_declaration(path, name='torch-base', release='r1', platform='linux/amd64', accelerator='none'):
    return {'schema_version':1,'name':name,'release':release,'artifacts':[
        {'kind':'sif','uri':Path(path).as_uri(),'platform':platform,'accelerator':accelerator,
         'tools':{'python':'python3','shell':'bash'}}]}
