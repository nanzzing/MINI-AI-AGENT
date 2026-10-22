"""이 앱 전용 인증 저장소. Codex/브라우저의 기존 로그인 정보를 가져오지 않는다."""
from contextlib import contextmanager
from copy import deepcopy
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile

from filelock import FileLock


def _windows_protect(data, decrypt=False):
    """Windows DPAPI: 현재 Windows 사용자만 복호화할 수 있게 저장한다."""
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN: 백그라운드 서버에서 OS 대화상자를 띄우지 않는다.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError('Windows credential protection failed')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(result.data)


class CredentialStore:
    def __init__(self, directory=None):
        root = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.config')
        self.directory = Path(directory) if directory else root / 'MINI-AI-AGENT' / 'chatgpt'
        self.path = self.directory / 'credentials.dat'
        self.lock = FileLock(str(self.directory / 'credentials.lock'), timeout=30)

    @contextmanager
    def locked(self):
        """프로세스 간 잠금으로 회전하는 refresh token의 동시 갱신을 막는다."""
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock:
            if self.path.exists():
                raw = self.path.read_bytes()
                if os.name == 'nt':
                    raw = _windows_protect(raw, decrypt=True)
                data = json.loads(raw)
            else:
                data = {'host_id': None, 'active': None, 'profiles': {}}
            before = deepcopy(data)
            yield data
            if data != before:
                raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
                if os.name == 'nt':
                    raw = _windows_protect(raw)
                # 같은 디렉터리에 임시 파일을 만든 뒤 원자적으로 교체한다.
                descriptor, name = tempfile.mkstemp(dir=self.directory, prefix='.credentials-')
                try:
                    with os.fdopen(descriptor, 'wb') as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(name, 0o600)
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
