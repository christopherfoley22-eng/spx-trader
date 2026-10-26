"""OFFLINE TEST VECTOR GENERATOR, not a product authenticator or verifier.

Uses cryptography to produce genuine P-256 signatures and cbor2 for standard
WebAuthn fixture encoding. Product code never imports or trusts this module.
"""
import hashlib
import json
import secrets
import struct
import cbor2
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from webauthn.helpers import bytes_to_base64url


class OfflineAuthenticator:
    def __init__(self,rp_id='mortificatio.test',origin='https://mortificatio.test'):
        self.rp_id,self.origin=rp_id,origin
        self.key=ec.generate_private_key(ec.SECP256R1())
        self.credential=secrets.token_bytes(32)
        self.counter=0;self.handle=None

    def _client(self,kind,expected_challenge,**changes):
        value=dict(type=kind,challenge=expected_challenge,origin=self.origin,crossOrigin=False)
        value.update(changes)
        return json.dumps(value,separators=(',',':')).encode()

    def registration(self,options,flags=0x45,**client_changes):
        self.handle=options['user']['id']
        public=self.key.public_key().public_numbers()
        cose=cbor2.dumps({1:2,3:-7,-1:1,-2:public.x.to_bytes(32,'big'),-3:public.y.to_bytes(32,'big')})
        data=hashlib.sha256(self.rp_id.encode()).digest()+bytes([flags])+struct.pack('>I',0)+b'\0'*16+struct.pack('>H',len(self.credential))+self.credential+cose
        client=self._client('webauthn.create',options['challenge'],**client_changes)
        identity=bytes_to_base64url(self.credential)
        return dict(id=identity,rawId=identity,type='public-key',authenticatorAttachment='cross-platform',
                    response=dict(clientDataJSON=bytes_to_base64url(client),attestationObject=bytes_to_base64url(cbor2.dumps({'fmt':'none','attStmt':{},'authData':data})),transports=['usb']),clientExtensionResults={})

    def assertion(self,options,flags=0x05,counter=None,rp_id=None,user_handle=None,**client_changes):
        if counter is None:self.counter+=1;counter=self.counter
        client=self._client('webauthn.get',options['challenge'],**client_changes)
        data=hashlib.sha256((rp_id or self.rp_id).encode()).digest()+bytes([flags])+struct.pack('>I',counter)
        signature=self.key.sign(data+hashlib.sha256(client).digest(),ec.ECDSA(hashes.SHA256()))
        identity=bytes_to_base64url(self.credential)
        return dict(id=identity,rawId=identity,type='public-key',authenticatorAttachment='cross-platform',
                    response=dict(clientDataJSON=bytes_to_base64url(client),authenticatorData=bytes_to_base64url(data),
                                  signature=bytes_to_base64url(signature),userHandle=self.handle if user_handle is None else user_handle),clientExtensionResults={})
