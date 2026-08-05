# ARV-001 runtime tree root

The official b10240 provenance attestation hashed the complete archive extraction
tree relative to the extraction root. The archive contains one top-level
`llama-b10240/` directory, so that path component is part of every canonical tree
record.

Hashing only the directory that contains `llama-server` omits the top-level
archive path. It preserves the same file and symlink counts but produces a
different tree digest. The repository validator therefore normalizes an official
`llama-b10240` bundle directory to its single-entry extraction root before
canonical serialization.

The extraction root must contain exactly one non-symlink top-level directory,
`llama-b10240`. The executable, archive digest, complete extraction-tree digest,
file count and symlink count remain bound by
`config/arv001/approved_local_runtime.json`.
