//! Small, host-only image utilities adapted from iOSPlus10's MIT-licensed op10tool.
//! Device flashing, Negroni USB control, and Negroni-specific UFS bootlog commands are excluded.
mod im4p;
mod ipsw;

use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(name = "lavender-image-tools", version, about = "Host-only IPSW/IM4P utilities for Lavender")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Download named files from a remote IPSW ZIP using ranged requests and CRC-32 checks.
    IpswGet {
        url: String,
        outdir: PathBuf,
        names: Vec<String>,
        #[arg(short = 'j', long, default_value_t = 32)]
        jobs: usize,
        #[arg(long, default_value_t = 32)]
        chunk_mib: u64,
    },
    /// Extract an IM4P payload (raw or LZFSE); unsupported compression is rejected.
    Im4p { input: PathBuf, output: PathBuf },
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::IpswGet { url, outdir, names, jobs, chunk_mib } => {
            ipsw::run(&url, &outdir, &names, jobs, chunk_mib << 20)
        }
        Cmd::Im4p { input, output } => im4p::run(&input, &output),
    }
}
