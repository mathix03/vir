"""Managed template credentials encrypted for the current Windows user with DPAPI."""
import ctypes
import os
import re
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Le coffre des modeles gere les identifiants avec Windows DPAPI.')
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    free = ctypes.WinDLL('kernel32').LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free(ctypes.cast(result.data, ctypes.c_void_p))


def path_for(root, template_id):
    if not re.fullmatch(r'[a-f0-9]{16}', template_id):
        raise ValueError('Identifiant de coffre invalide.')
    return Path(root) / 'private' / 'template-credentials' / (template_id + '.dpapi')


def store(root, template_id, password):
    path = path_for(root, template_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(crypt(password.encode('utf-8')))
    temporary.replace(path)


def load(root, template_id):
    return crypt(path_for(root, template_id).read_bytes(), decrypt=True).decode('utf-8')
