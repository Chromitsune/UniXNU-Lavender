# Third-party dependency notice

This crate downloads Rust dependencies from crates.io; it does not vendor or copy
their source code. Direct dependencies are `anyhow`, `clap`, `crc32fast`, `flate2`,
`lzfse_rust`, `rayon`, `simple_asn1`, `ureq`, and `zip`. `Cargo.lock` records the
resolved versions. Each dependency retains its own license and copyright; inspect
its crates.io metadata and license before redistributing a binary that includes it.
