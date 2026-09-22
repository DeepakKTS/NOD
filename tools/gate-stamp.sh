#!/bin/sh
# The content hash of every file the gate actually saw.
#
# Two things this must get right, and the second was learned the hard way:
#
# 1. **Working tree, not index.** `git ls-files -s` reports *index* blobs, so a
#    file modified and not staged hashes the same as before the edit, and a
#    stamp built from it would call a dirty tree clean.
# 2. **Tracked AND untracked-but-not-ignored.** A stamp over tracked files only
#    changes the moment `git add` promotes a new file, so `make gate` then
#    `git add` then `git commit` was refused even though nothing had been
#    edited. Refusing is the safe direction, but a guard that cries wolf on the
#    normal workflow is a guard someone learns to skip — which is how the last
#    two died. `-c -o --exclude-standard` sees the same set either side of the
#    `git add`.
# 3. **Sorted.** Fixing (2) at the level of the *set* left the same bug at the
#    level of the *order*: `git ls-files -c -o` emits cached entries and then
#    others, each group sorted but the groups concatenated, so `git add`
#    **moves** a file within the list and the digest changes though not one byte
#    did. Observed at Gate 4a on `make gate; git add <new file>; git commit`,
#    which is the exact workflow (2) was written to stop refusing. Same symptom,
#    same consequence, one level down — so the listing is sorted before hashing
#    and the digest depends on content alone.
set -eu
git ls-files -z -c -o --exclude-standard \
    | sort -z \
    | xargs -0 shasum -a 256 2>/dev/null \
    | shasum -a 256 | cut -d' ' -f1
