#!/usr/bin/env python3
"""Exercise receiver-side copy-on-write replacement files."""

import os
import errno
import fcntl
import struct
import sys
import time

from rsyncfns import FROMDIR, TODIR, run_rsync, test_fail, test_skipped


FIEMAP = 0xC020660B
FIEMAP_EXTENT_SIZE = 56


def physical_extents(path):
    """Return (physical start, length) pairs reported by Linux FIEMAP."""
    header = struct.pack('<QQIIII', 0, 0xFFFFFFFFFFFFFFFF, 0, 0, 256, 0)
    request = bytearray(header + bytes(256 * FIEMAP_EXTENT_SIZE))
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.ioctl(fd, FIEMAP, request, True)
    except OSError as exc:
        if exc.errno in (errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP):
            test_skipped('filesystem does not provide FIEMAP')
        raise
    finally:
        os.close(fd)
    mapped = struct.unpack_from('<I', request, 20)[0]
    extents = []
    for index in range(mapped):
        offset = 32 + index * FIEMAP_EXTENT_SIZE
        physical = struct.unpack_from('<Q', request, offset + 8)[0]
        length = struct.unpack_from('<Q', request, offset + 16)[0]
        extents.append((physical, length))
    return extents


def has_shared_extent(left, right):
    for left_start, left_length in left:
        left_end = left_start + left_length
        for right_start, right_length in right:
            right_end = right_start + right_length
            if max(left_start, right_start) < min(left_end, right_end):
                return True
    return False


if not sys.platform.startswith('linux'):
    test_skipped('reflink support is Linux-specific')

source = FROMDIR / 'file'
dest = TODIR / 'file'
FROMDIR.mkdir(parents=True, exist_ok=True)
TODIR.mkdir(parents=True, exist_ok=True)
# Use unique blocks so the unchanged suffix can only match at its original
# offset. Change a complete initial block to exercise extent preservation.
basis = b''.join((f'{index:04d}'.encode() * 256)[:1024]
                 for index in range(1024))
source.write_bytes(b'B' * 65536 + basis[65536:])
dest.write_bytes(basis)
now = time.time()
os.utime(dest, (now - 20, now - 20))
os.utime(source, (now, now))

# Keep the old basis so a successful FICLONE leaves shared extents observable
# to filesystem tooling, while the transfer itself exercises the normal delta.
run_rsync('-a', '--no-whole-file', '--update', '--backup', '--suffix=.old',
          '--reflink=always', f'{FROMDIR}/', f'{TODIR}/')

if dest.read_bytes() != source.read_bytes():
    test_fail('reflink transfer produced incorrect content')
if (TODIR / 'file.old').read_bytes() != basis:
    test_fail('reflink transfer did not preserve the old basis')
if not has_shared_extent(physical_extents(dest), physical_extents(TODIR / 'file.old')):
    test_fail('reflink transfer did not preserve any shared extents')

# An aligned insertion moves the matching ranges. With a matching block size,
# this exercises FICLONERANGE rather than the same-offset seek path.
moved_source = FROMDIR / 'moved'
moved_dest = TODIR / 'moved'
moved_source.write_bytes(b'X' * 4096 + basis)
moved_dest.write_bytes(basis)
os.utime(moved_dest, (now - 20, now - 20))
run_rsync('-a', '--no-whole-file', '--block-size=4096', '--backup',
          '--suffix=.moved-old', '--reflink=always', f'{FROMDIR}/', f'{TODIR}/')
if moved_dest.read_bytes() != moved_source.read_bytes():
    test_fail('range-reflink transfer produced incorrect content')
if not has_shared_extent(physical_extents(moved_dest),
                         physical_extents(TODIR / 'moved.moved-old')):
    test_fail('range-reflink transfer did not preserve shared extents')

# New files have no destination basis to clone, but must still be created
# normally when reflink=always is requested.
new_source = FROMDIR / 'new' / 'nested' / 'file'
new_dest = TODIR / 'new' / 'nested' / 'file'
new_source.parent.mkdir(parents=True, exist_ok=True)
new_source.write_bytes(b'new file without a destination basis\n')
run_rsync('-a', '--reflink=always', f'{FROMDIR}/', f'{TODIR}/')
if new_dest.read_bytes() != new_source.read_bytes():
    test_fail('reflink transfer did not create a new nested file')

# --update must still skip a destination that is newer than the source.
source.write_bytes(b'C' + source.read_bytes()[1:])
os.utime(source, (now + 20, now + 20))
os.utime(dest, (now + 40, now + 40))
run_rsync('-a', '--update', '--reflink=auto', f'{FROMDIR}/', f'{TODIR}/')
if dest.read_bytes() == source.read_bytes():
    test_fail('--update overwrote a newer destination')

# Reflinking and in-place replacement have conflicting semantics: the former
# needs a new temporary inode while the latter deliberately updates the old
# inode. Keep the interface explicit rather than silently ignoring --reflink.
rejected = run_rsync('--reflink=auto', '--inplace', f'{FROMDIR}/', f'{TODIR}/',
                     check=False, capture_output=True)
if rejected.returncode == 0 or '--reflink cannot be used with --inplace' not in rejected.stderr:
    test_fail('--reflink and --inplace were not rejected together')

print('reflink: CoW replacement, range preservation, --update, and option validation passed')
