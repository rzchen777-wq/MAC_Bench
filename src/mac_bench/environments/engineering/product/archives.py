"""Source and submitted-artifact archive validation."""
import tarfile
import posixpath
from pathlib import PurePosixPath

def check_archive(path, *, external_symlink_targets=()):
    """Validate sources, or artifacts with explicitly provided interpreter links.

    Source callers never opt in. Trusted artifact contracts may name exact
    existing consumer files; writing archive members through any link remains
    forbidden, including links to these files.
    """
    allowed = set(external_symlink_targets)
    if any(not PurePosixPath(p).is_absolute() or '..' in PurePosixPath(p).parts for p in allowed):
        raise ValueError('External symlink targets must be exact absolute paths')
    with tarfile.open(path) as archive:
        names = set()
        links = {}
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or member.name in names:
                raise ValueError("Unsafe or duplicate archive member: " + member.name)
            if member.issym():
                target = posixpath.normpath(posixpath.join(str(name.parent), member.linkname))
                if (member.linkname.startswith("/") or target == ".." or target.startswith("../")) and member.linkname not in allowed:
                    raise ValueError("Escaping archive symlink: " + member.name)
                links[str(name)] = target
            elif not member.isfile() and not member.isdir():
                raise ValueError("Unsupported archive member: " + member.name)
            if ".git" in name.parts:
                raise ValueError("Source history must not enter agent workspace")
            names.add(member.name)
        # Never write a later member through a symlink created by the archive.
        for member in archive.getmembers():
            if any(str(parent) in links for parent in PurePosixPath(member.name).parents):
                raise ValueError("Archive member has a symlink parent: " + member.name)
        for source, target in links.items():
            visited = {source}
            while target in links:
                if target in visited:
                    raise ValueError("Archive contains a symlink cycle")
                visited.add(target)
                target = links[target]
