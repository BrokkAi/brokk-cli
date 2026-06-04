from __future__ import annotations

import hashlib
import os
import struct
import zlib
from pathlib import Path

ROOT = Path('.')
GIT_DIR = ROOT / '.git'
INDEX_PATH = GIT_DIR / 'index'


def read_head() -> str:
    head = (GIT_DIR / 'HEAD').read_text().strip()
    if head.startswith('ref: '):
        ref = head.split(':', 1)[1].strip()
        ref_path = GIT_DIR / ref
        return ref_path.read_text().strip()
    return head


def read_object(sha: str) -> bytes:
    obj_path = GIT_DIR / 'objects' / sha[:2] / sha[2:]
    if not obj_path.exists():
        # packed objects not handled
        raise FileNotFoundError(obj_path)
    compressed = obj_path.read_bytes()
    raw = zlib.decompress(compressed)
    null = raw.index(b'\x00')
    header = raw[:null].decode('ascii')
    body = raw[null + 1 :]
    _, size = header.split(' ')
    if len(body) != int(size):
        pass
    return body


def parse_tree(data: bytes) -> dict[str, str]:
    """Return path -> sha1 hex for tree entries (recursive not handled)."""
    result: dict[str, str] = {}
    i = 0
    while i < len(data):
        space = data.index(b' ', i)
        mode = data[i:space]
        i = space + 1
        nul = data.index(b'\x00', i)
        name = data[i:nul].decode('utf-8')
        i = nul + 1
        sha = data[i:i + 20].hex()
        i += 20
        result[name] = sha
    return result


def parse_commit(data: bytes) -> str:
    # find b'tree <sha>\n'
    for line in data.split(b'\n'):
        if line.startswith(b'tree '):
            return line.split()[1].decode('ascii')
    raise ValueError('tree not found')


def parse_index_entry(fh) -> tuple[str, str]:
    # returns path, sha
    stat = fh.read(100)
    if len(stat) < 100:
        raise EOFError
    # skip from ctime through size
    _ = struct.unpack('!LLLLLLLLLL', stat[:40])
    sha = fh.read(20).hex()
    flags = struct.unpack('!H', fh.read(2))[0]
    name_len = flags & 0x0FFF
    path_bytes = bytearray()
    while True:
        b = fh.read(1)
        if b == b'\x00':
            break
        path_bytes.extend(b)
    path = path_bytes.decode('utf-8')
    # align to 8-byte boundary
    pos = fh.tell()
    padding = (8 - (pos % 8)) % 8
    if padding:
        fh.read(padding)
    return path, sha


def parse_index() -> dict[str, str]:
    data = INDEX_PATH.read_bytes()
    if data[:4] != b'DIRC':
        raise ValueError('bad index')
    version, count = struct.unpack('!II', data[4:12])
    if version not in (2,3,4):
        raise ValueError(f'unsupported index version {version}')
    out = {}
    from io import BytesIO
    fh = BytesIO(data[12:])
    for _ in range(count):
        path, sha = parse_index_entry(fh)
        out[path] = sha
    return out


def blob_hash(path: Path) -> str:
    data = path.read_bytes()
    h = hashlib.sha1()
    h.update(f'blob {len(data)}\0'.encode('ascii'))
    h.update(data)
    return h.hexdigest()


def status():
    if not INDEX_PATH.exists():
        print('No index file')
        return
    index = parse_index()
    try:
        head_sha = read_head()
        head_tree = {}
        if head_sha:
            commit = read_object(head_sha)
            tree_sha = parse_commit(commit)
            head_tree = parse_tree(read_object(tree_sha))
    except Exception:
        head_tree = {}

    paths = set(index) | set(head_tree)
    staged = {}
    unstaged = {}

    for p in sorted(paths):
        i = index.get(p)
        h = head_tree.get(p)
        wt = None
        fs = Path(p)
        if fs.exists():
            if fs.is_file():
                wt = blob_hash(fs)
            else:
                continue
        # staged
        if i != h:
            if h is None and i is not None:
                staged[p] = 'added'
            elif h is not None and i is None:
                staged[p] = 'deleted'
            else:
                staged[p] = 'modified'
        # unstaged (index vs worktree)
        if wt != i:
            if i is None:
                if wt is not None:
                    # untracked
                    unstaged[p] = 'untracked'
            elif wt is None:
                unstaged[p] = 'deleted'
            else:
                unstaged[p] = 'modified'

    # untracked files: in working tree but not index nor HEAD
    for fs in ROOT.rglob('*'):
        if '.git' in fs.parts:
            continue
        if fs.is_file():
            rel = fs.relative_to(ROOT).as_posix()
            if rel not in index and rel not in head_tree:
                unstaged[rel] = unstaged.get(rel, 'untracked')

    print('STAGED', staged)
    print('UNSTAGED', unstaged)

if __name__ == '__main__':
    status()
