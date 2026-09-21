#!/bin/sh
# The content hash of every tracked file, as they are on disk right now.
#
# `git ls-files -s` is not enough: it reports *index* blobs, so a file modified
# and not staged hashes the same as before the edit, and a stamp built from it
# would call a dirty tree clean. This hashes the working tree.
set -eu
git ls-files -z | xargs -0 shasum -a 256 2>/dev/null | shasum -a 256 | cut -d' ' -f1
