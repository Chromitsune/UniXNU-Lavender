# Lavender host image tools

This isolated Rust CLI provides only two host-side helpers: `ipsw-get` and `im4p`.
They were adapted from `tools/op10tool` in `Chromitsune/iOSPlus10` at commit
`505ae76ba72f82c336e2a194b47a3145cdbbcf17` (MIT; see this directory's `LICENSE`).
No UEFI patches, flashing commands, Negroni `ctl` protocol, or Negroni UFS bootlog
format were imported. The upstream `op10tool bootlog` cannot parse Lavender's RAM
ring/ESP log.

The IPSW URL and members used by the Lavender board are in `board.toml`. For example:

```sh
CARGO_TARGET_DIR=out/lavender/image-tools-target cargo run --locked --release \
  --manifest-path tools/lavender-image-tools/Cargo.toml -- ipsw-get 'https://updates.cdn-apple.com/2025FallFCS/fullrestores/122-77006/1158E502-D76D-4226-AA06-ED5E30CD1111/iPhone10,3,iPhone10,6_16.7.16_20H392_Restore.ipsw' \
  out/lavender/ipsw KernelCache DeviceTree.d22ap.im4p
```

`ipsw-get` fetches only the requested ZIP members, resumes completed chunks, and
checks their CRC-32. `im4p` extracts raw or LZFSE payloads; it rejects unsupported
`complzss` data rather than guessing. Apple artifacts and build outputs belong in
ignored `out/`, never in Git. `scripts/lavender-image-tools.sh` wraps this invocation. These tools do not access or modify a device.

Third-party Rust crates are fetched by Cargo and are not vendored here; retain each
crate's own license when packaging a binary.
