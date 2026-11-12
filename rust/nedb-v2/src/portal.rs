// SPDX-FileCopyrightText: 2026 INTERCHAINED LLC
// SPDX-License-Identifier: BUSL-1.1
// NEDB · © 2026 INTERCHAINED LLC × Eth-Interchained × Vex (Claude Opus 5)

//! portal.rs — NEDB Portals: sharded-on-disk, referenced-not-loaded storage.
//!
//! A **portal** is a reference to one sealed segment + its `.idx` sidecar.
//! The shard's data never moves and is never loaded wholesale: a small
//! registry manifest (`portals.json`, in the segments dir) lists portals by
//! file name, entry count, optional seq range, and the `.idx` checksum
//! recorded at registration.
//!
//! Memory bounds (the point of the design — Vision's in-memory embedded
//! store OOMs on open because it rehydrates the whole materialized store):
//!   * Registry open cost = size of `portals.json` (KBs). It reads ONLY the
//!     manifest — it does not open, scan, or stat segment/idx contents.
//!   * Portal open cost = one 12-byte `.idx` header read + file lengths.
//!   * Lookup cost = O(log n) positional reads of 44-byte `.idx` entries,
//!     binary-searching the `.idx` ON DISK. The `.idx` is never loaded.
//!   * Read cost = lookup + one positional segment read, BLAKE2b-verified
//!     with the same helper `segment.rs` uses (tamper-evidence unchanged).
//!
//! Invariant: `.idx` entries are sorted by raw digest. `SegmentStore`'s
//! seal path (`write_idx`) sorts at seal time; registration re-validates
//! sortedness + the sidecar checksum by streaming the `.idx` once (seal-time
//! cost, O(1) memory) and refuses an unsorted sidecar, so `lookup` can trust
//! the order without ever re-reading the whole file. Per-lookup integrity
//! does not depend on the `.idx`: a wrong/corrupt entry can at worst cause a
//! miss, never bad bytes, because every record read is hash-verified.
//!
//! Additive only: nothing in the default v2/v3 store paths calls this
//! module yet. Writes, an active (appending) portal, cross-portal queries,
//! and Python-engine parity are deliberately NOT in this slice — see
//! `docs/portals.md`.

use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use anyhow::{bail, Context, Result};
use blake2::{Blake2b512, Digest};
use serde::{Deserialize, Serialize};

use crate::segment::{
    blake2b_raw, read_at, SegmentStore, IDX_CHECKSUM_BYTES, IDX_ENTRY_BYTES, IDX_HEADER_BYTES,
    IDX_MAGIC,
};

/// Manifest file name, inside the segments dir it describes.
pub const MANIFEST_FILE: &str = "portals.json";

/// Addressing semantics for a portal's `.idx` digests (Oracle review
/// 2026-11-11: these are two different semantics and the format must say
/// which one a portal uses, versioned, never implied).
pub const ADDRESSING_CONTENT_DIGEST: &str = "content-digest";

/// Tombstone marker (Oracle invariant 1, 2026-11-11), shared with the
/// Python core: in a KEY-HASH shard, an `.idx` entry whose len is
/// TOMBSTONE_LEN records a DELETION (offset 0, no segment record).
/// Multi-shard reads go newest-first and a tombstone stops the search —
/// older values must never resurface. Content-digest shards never carry
/// tombstones: a digest names immutable content, so there is no newer
/// version of it to win.
pub const TOMBSTONE_LEN: u32 = u32::MAX;
/// `.idx` digest = BLAKE2b-256(key); records carry the value's digest.
/// Python KV shards.
pub const ADDRESSING_KEY_HASH: &str = "key-hash";

fn default_addressing() -> String {
    ADDRESSING_CONTENT_DIGEST.to_string()
}

/// One portal entry in the registry manifest. Paths are FILE NAMES relative
/// to the registry dir: portal shards live beside the manifest, so a store
/// dir stays relocatable as a unit.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct PortalEntry {
    /// Caller-chosen portal id (e.g. a Vision height range like "h0-99999").
    pub id: String,
    /// Segment file name (e.g. "seg-000000.dat").
    pub segment: String,
    /// Index sidecar file name (e.g. "seg-000000.idx").
    pub idx: String,
    /// Number of records indexed by the sidecar.
    pub entries: u64,
    /// Optional sequence range covered by this shard, when the caller knows
    /// it (segment.rs itself does not track seqs).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub seq_min: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub seq_max: Option<u64>,
    /// Hex BLAKE2b-256 of the `.idx` body (header + entries), as recorded in
    /// the sidecar's own checksum trailer and verified at registration.
    pub idx_checksum: String,
    /// Lookup semantics of this portal's digests: "content-digest"
    /// (default; `.idx` digest = BLAKE2b-256 of the record content, the
    /// Rust DAG addressing) or "key-hash" (`.idx` digest =
    /// BLAKE2b-256(key), the Python KV addressing). Explicit in the
    /// manifest so a reader never has to guess which semantics a shard
    /// uses. Readers must refuse a semantics they do not implement.
    #[serde(default = "default_addressing")]
    pub addressing: String,
    /// Optional base locator for the shard (Mark's decision 2026-11-11:
    /// local now, registry shaped so remote is additive later). Absent =
    /// `segment`/`idx` are file names in the registry dir (the only
    /// implemented location in this slice). A future remote portal sets
    /// this to a base URI and readers resolve the file names against it;
    /// readers that do not implement remote locations must refuse the
    /// portal, never guess.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uri: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Manifest {
    version: u32,
    portals: Vec<PortalEntry>,
}

/// The portal registry: the ONLY thing a cold open has to read.
#[derive(Clone, Debug)]
pub struct PortalRegistry {
    dir: PathBuf,
    entries: Vec<PortalEntry>,
}

impl PortalRegistry {
    /// Create an empty registry rooted at `dir` (no manifest written until
    /// `save`; no segment/idx file is touched).
    pub fn new(dir: &Path) -> Self {
        Self { dir: dir.to_path_buf(), entries: Vec::new() }
    }

    /// Open the registry under `dir` by reading `portals.json` ONLY.
    /// Segment and `.idx` files are not opened, read, or required to exist.
    pub fn open(dir: &Path) -> Result<Self> {
        let path = dir.join(MANIFEST_FILE);
        let raw = fs::read(&path).with_context(|| format!("read portal manifest {:?}", path))?;
        let m: Manifest = serde_json::from_slice(&raw).context("parse portal manifest")?;
        if m.version != 1 {
            bail!("unsupported portal manifest version {}", m.version);
        }
        Ok(Self { dir: dir.to_path_buf(), entries: m.portals })
    }

    /// Persist the manifest atomically (tmp → rename), like `.idx` writes.
    pub fn save(&self) -> Result<()> {
        fs::create_dir_all(&self.dir).context("create portal registry dir")?;
        let m = Manifest { version: 1, portals: self.entries.clone() };
        let raw = serde_json::to_string_pretty(&m).context("serialize portal manifest")?;
        let path = self.dir.join(MANIFEST_FILE);
        let tmp = path.with_extension("json.tmp");
        // Invariant 2 (crash-safe registration): Write -> Seal -> Verify
        // -> fsync -> Atomic registry commit. fsync the temp manifest,
        // rename it atomically over the old one, then fsync the
        // directory — a crash at any point leaves the old manifest or
        // the new one, never a torn reference to an unverified shard.
        {
            use std::io::Write;
            let mut f = fs::File::create(&tmp).context("create temp manifest")?;
            f.write_all(raw.as_bytes())?;
            f.sync_all().context("fsync temp manifest")?;
        }
        fs::rename(&tmp, &path).context("commit portal manifest")?;
        if let Ok(d) = fs::File::open(&self.dir) {
            let _ = d.sync_all(); // directory fsync: best effort by platform
        }
        Ok(())
    }

    pub fn entries(&self) -> &[PortalEntry] {
        &self.entries
    }

    /// Drop entries whose segment file no longer exists under `dir`
    /// (deleted or compacted away after the entry was registered), so a
    /// persisted registry cannot accumulate references to gone segments.
    pub fn prune_missing(&mut self) {
        let dir = self.dir.clone();
        self.entries.retain(|e| dir.join(&e.segment).exists());
    }

    pub fn get(&self, id: &str) -> Option<&PortalEntry> {
        self.entries.iter().find(|e| e.id == id)
    }

    /// Register a sealed segment (by numeric id, `seg-{id:06}.dat/.idx` in
    /// this registry's dir) as a portal. Streams the `.idx` once to verify
    /// its checksum trailer and the sorted-by-digest invariant, and to count
    /// entries — O(file) time at seal/registration, O(1) memory, and the
    /// segment file itself is never read. Re-registering an id replaces it.
    pub fn register_segment(
        &mut self,
        id: &str,
        segment_id: u32,
        seq_range: Option<(u64, u64)>,
        addressing: &str,
    ) -> Result<PortalEntry> {
        if addressing != ADDRESSING_CONTENT_DIGEST && addressing != ADDRESSING_KEY_HASH {
            bail!("portal {}: unknown addressing semantics {:?}", id, addressing);
        }
        let seg_path = SegmentStore::seg_path(&self.dir, segment_id);
        let idx_path = SegmentStore::idx_path(&self.dir, segment_id);
        if !seg_path.exists() {
            bail!("portal {}: segment {:?} does not exist", id, seg_path);
        }
        let (entries, checksum) = verify_idx_streaming(&idx_path)?;
        let entry = PortalEntry {
            id: id.to_string(),
            segment: seg_path.file_name().unwrap().to_string_lossy().into_owned(),
            idx: idx_path.file_name().unwrap().to_string_lossy().into_owned(),
            entries,
            seq_min: seq_range.map(|r| r.0),
            seq_max: seq_range.map(|r| r.1),
            idx_checksum: checksum,
            addressing: addressing.to_string(),
            uri: None,
        };
        self.entries.retain(|e| e.id != id);
        self.entries.push(entry.clone());
        Ok(entry)
    }

    /// Open one registered portal for on-disk lookups. Reads only the 12-byte
    /// `.idx` header (+ file lengths); loads nothing.
    pub fn open_portal(&self, id: &str) -> Result<Portal> {
        let entry = self
            .get(id)
            .with_context(|| format!("portal {} not in registry", id))?
            .clone();
        Portal::open(&self.dir, entry)
    }
}

/// Stream-verify an `.idx`: header magic, exact length, checksum trailer,
/// strictly increasing digests. Returns (entry_count, body_checksum_hex).
fn verify_idx_streaming(idx_path: &Path) -> Result<(u64, String)> {
    let mut f = File::open(idx_path).with_context(|| format!("open idx {:?}", idx_path))?;
    let file_len = f.metadata()?.len();
    if file_len < (IDX_HEADER_BYTES + IDX_CHECKSUM_BYTES) as u64 {
        bail!("idx {:?} too short: {} bytes", idx_path, file_len);
    }
    let mut header = [0u8; IDX_HEADER_BYTES];
    f.read_exact(&mut header)?;
    if &header[0..4] != IDX_MAGIC {
        bail!("idx {:?}: bad magic", idx_path);
    }
    let count = u64::from_le_bytes(header[4..12].try_into().unwrap());
    let expected = (IDX_HEADER_BYTES + IDX_CHECKSUM_BYTES) as u64 + count * IDX_ENTRY_BYTES as u64;
    if file_len != expected {
        bail!("idx {:?}: length {} != expected {} for {} entries", idx_path, file_len, expected, count);
    }
    let mut hasher = Blake2b512::new();
    hasher.update(header);
    let mut prev: Option<[u8; 32]> = None;
    let mut buf = [0u8; IDX_ENTRY_BYTES];
    for i in 0..count {
        f.read_exact(&mut buf)?;
        hasher.update(buf);
        let digest: [u8; 32] = buf[..32].try_into().unwrap();
        if let Some(p) = prev {
            if digest <= p {
                bail!(
                    "idx {:?}: entry {} not sorted by digest — re-seal the segment \
                     (current seal path sorts) before registering it as a portal",
                    idx_path, i
                );
            }
        }
        prev = Some(digest);
    }
    let mut trailer = [0u8; IDX_CHECKSUM_BYTES];
    f.read_exact(&mut trailer)?;
    // The trailer is BLAKE2b-256(body) = first 32 bytes of Blake2b-512(body),
    // exactly what the streaming hasher accumulated over header + entries.
    let out = hasher.finalize();
    if out[..32] != trailer[..] {
        bail!("idx {:?}: checksum mismatch", idx_path);
    }
    Ok((count, hex::encode(trailer)))
}

/// An open portal: two file handles + the entry count. Nothing else is held
/// in memory, regardless of shard size.
pub struct Portal {
    entry: PortalEntry,
    idx: File,
    segment: File,
    count: u64,
    /// Positional `.idx` entry reads performed by `lookup` (diagnostics and
    /// tests: a lookup must stay O(log n), never O(n)).
    idx_reads: AtomicUsize,
}

impl Portal {
    /// Open a portal from its registry entry. Header-length sanity only.
    pub fn open(dir: &Path, entry: PortalEntry) -> Result<Self> {
        if entry.uri.is_some() {
            bail!(
                "portal {}: remote locator {:?} not implemented in this slice (local portals only)",
                entry.id, entry.uri
            );
        }
        if entry.addressing != ADDRESSING_CONTENT_DIGEST {
            bail!(
                "portal {}: addressing {:?} not implemented by this reader (content-digest only)",
                entry.id, entry.addressing
            );
        }
        let idx_path = dir.join(&entry.idx);
        let seg_path = dir.join(&entry.segment);
        let idx = File::open(&idx_path).with_context(|| format!("open portal idx {:?}", idx_path))?;
        let segment =
            File::open(&seg_path).with_context(|| format!("open portal segment {:?}", seg_path))?;
        let mut header = [0u8; IDX_HEADER_BYTES];
        read_at(&idx, &mut header, 0).context("read portal idx header")?;
        if &header[0..4] != IDX_MAGIC {
            bail!("portal {}: idx bad magic", entry.id);
        }
        let count = u64::from_le_bytes(header[4..12].try_into().unwrap());
        if count != entry.entries {
            bail!("portal {}: idx count {} != manifest entries {}", entry.id, count, entry.entries);
        }
        let idx_len = idx.metadata()?.len();
        let expected = (IDX_HEADER_BYTES + IDX_CHECKSUM_BYTES) as u64 + count * IDX_ENTRY_BYTES as u64;
        if idx_len != expected {
            bail!("portal {}: idx length {} != expected {}", entry.id, idx_len, expected);
        }
        Ok(Self { entry, idx, segment, count, idx_reads: AtomicUsize::new(0) })
    }

    pub fn entry(&self) -> &PortalEntry {
        &self.entry
    }

    pub fn count(&self) -> u64 {
        self.count
    }

    /// Total positional `.idx` entry reads this portal has performed.
    pub fn idx_reads(&self) -> usize {
        self.idx_reads.load(Ordering::Relaxed)
    }

    /// Binary-search the `.idx` ON DISK for `digest` (raw BLAKE2b-256).
    /// Returns the record's (content_offset, len) in the segment, or None.
    /// Cost: ceil(log2(count)) positional reads of 44 bytes each.
    pub fn lookup(&self, digest: &[u8; 32]) -> Result<Option<(u64, u32)>> {
        let (mut lo, mut hi) = (0u64, self.count);
        let mut buf = [0u8; IDX_ENTRY_BYTES];
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            let pos = (IDX_HEADER_BYTES as u64) + mid * (IDX_ENTRY_BYTES as u64);
            read_at(&self.idx, &mut buf, pos).context("portal idx binary search read")?;
            self.idx_reads.fetch_add(1, Ordering::Relaxed);
            let found: &[u8; 32] = buf[..32].try_into().unwrap();
            match (*digest).cmp(found) {
                std::cmp::Ordering::Equal => {
                    let off = u64::from_le_bytes(buf[32..40].try_into().unwrap());
                    let len = u32::from_le_bytes(buf[40..44].try_into().unwrap());
                    return Ok(Some((off, len)));
                }
                std::cmp::Ordering::Less => hi = mid,
                std::cmp::Ordering::Greater => lo = mid + 1,
            }
        }
        Ok(None)
    }

    /// On-call read: disk binary search, one positional segment read, and a
    /// BLAKE2b re-verification (reusing segment.rs's digest helper). Returns
    /// None for an absent digest; errors if the stored bytes were tampered.
    /// All digests in this shard, streamed from the `.idx` in bounded
    /// chunks (enumeration is the one operation that touches every
    /// entry; it still never holds the whole index at once).
    pub fn digests(&self) -> Result<Vec<[u8; 32]>> {
        let mut out = Vec::with_capacity(self.count as usize);
        const CHUNK: u64 = 1024;
        let mut start: u64 = 0;
        while start < self.count {
            let n = CHUNK.min(self.count - start);
            let mut buf = vec![0u8; (n as usize) * IDX_ENTRY_BYTES];
            read_at(&self.idx, &mut buf,
                    IDX_HEADER_BYTES as u64 + start * IDX_ENTRY_BYTES as u64)?;
            for i in 0..n as usize {
                let mut d = [0u8; 32];
                d.copy_from_slice(&buf[i * IDX_ENTRY_BYTES..i * IDX_ENTRY_BYTES + 32]);
                out.push(d);
            }
            start += n;
        }
        Ok(out)
    }

    pub fn read(&self, digest: &[u8; 32]) -> Result<Option<Vec<u8>>> {
        let (off, len) = match self.lookup(digest)? {
            Some(loc) => loc,
            None => return Ok(None),
        };
        if len == TOMBSTONE_LEN {
            bail!("portal {}: tombstone entry in a content-digest shard \
                   (tombstones are a key-hash concept)", self.entry.id);
        }
        let mut content = vec![0u8; len as usize];
        read_at(&self.segment, &mut content, off).context("portal segment record read")?;
        if blake2b_raw(&content) != *digest {
            bail!("portal {}: record tampered (hash mismatch)", self.entry.id);
        }
        Ok(Some(content))
    }

    /// Hex-digest convenience wrapper matching `SegmentStore`'s API shape.
    pub fn read_hex(&self, hash_hex: &str) -> Result<Option<Vec<u8>>> {
        let raw = hex::decode(hash_hex).context("portal read: bad hash hex")?;
        let digest: [u8; 32] =
            raw.try_into().map_err(|_| anyhow::anyhow!("portal read: hash not 32 bytes"))?;
        self.read(&digest)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::segment::blake2b_raw as digest_of;
    use std::io::Write;
    use tempfile::tempdir;

    /// Registry open must succeed with NO segment/idx files present — proof
    /// it reads the manifest only.
    #[test]
    fn registry_open_reads_manifest_only() {
        let dir = tempdir().unwrap();
        let mut reg = PortalRegistry::new(dir.path());
        reg.entries.push(PortalEntry {
            id: "ghost".into(),
            segment: "seg-000042.dat".into(),
            idx: "seg-000042.idx".into(),
            entries: 7,
            seq_min: Some(1),
            seq_max: Some(9),
            idx_checksum: "ab".repeat(32),
            addressing: ADDRESSING_CONTENT_DIGEST.to_string(),
            uri: None,
        });
        reg.save().unwrap();
        // Note: seg-000042.* were never created.
        let reg2 = PortalRegistry::open(dir.path()).unwrap();
        assert_eq!(reg2.entries().len(), 1);
        assert_eq!(reg2.get("ghost").unwrap().entries, 7);
        assert!(reg2.open_portal("ghost").is_err(), "open_portal (not registry open) is what touches files");
    }

    /// A manifest entry carrying a remote `uri` round-trips through
    /// save/open (the shape is reserved), but opening that portal refuses
    /// cleanly in this local-only slice.
    #[test]
    fn remote_uri_is_reserved_and_refused() {
        let dir = tempdir().unwrap();
        let mut reg = PortalRegistry::new(dir.path());
        reg.entries.push(PortalEntry {
            id: "remote-1".into(),
            segment: "seg-000007.dat".into(),
            idx: "seg-000007.idx".into(),
            entries: 3,
            seq_min: None,
            seq_max: None,
            idx_checksum: "cd".repeat(32),
            addressing: ADDRESSING_CONTENT_DIGEST.to_string(),
            uri: Some("https://example.invalid/shards/".into()),
        });
        reg.save().unwrap();
        let reg2 = PortalRegistry::open(dir.path()).unwrap();
        assert_eq!(reg2.get("remote-1").unwrap().uri.as_deref(),
                   Some("https://example.invalid/shards/"));
        let err = reg2.open_portal("remote-1").err().unwrap().to_string();
        assert!(err.contains("not implemented"), "got: {}", err);
    }

    /// A tombstone entry (key-hash concept) inside a content-digest
    /// shard is a format violation: the read must refuse, not guess.
    #[test]
    fn tombstone_in_content_shard_is_refused() {
        let dir = tempdir().unwrap();
        let content = b"tombstone-target".to_vec();
        let digest = blake2b_raw(&content);
        let hex = hex::encode(digest);
        // Segment with one real record; idx entry lies with len=TOMBSTONE.
        let seg = SegmentStore::seg_path(dir.path(), 5);
        let mut bytes = Vec::new();
        bytes.extend_from_slice(&(content.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&content);
        fs::write(&seg, &bytes).unwrap();
        SegmentStore::write_idx(dir.path(), 5, &[(hex.clone(), 4, TOMBSTONE_LEN)])
            .unwrap();
        let mut reg = PortalRegistry::new(dir.path());
        reg.register_segment("seg-000005", 5, None, ADDRESSING_CONTENT_DIGEST)
            .unwrap();
        let portal = reg.open_portal("seg-000005").unwrap();
        let err = portal.read(&digest).err().unwrap().to_string();
        assert!(err.contains("tombstone"), "got: {}", err);
    }

    /// Build a real sealed segment through SegmentStore (tiny rollover), then
    /// serve its first shard through a portal.
    #[test]
    fn sealed_segment_portal_roundtrip_and_absent() {
        let dir = tempdir().unwrap();
        let root = dir.path().to_path_buf();
        let mut contents: Vec<Vec<u8>> = Vec::new();
        let mut hashes: Vec<String> = Vec::new();
        {
            let s = SegmentStore::open_with_max(&root, 32).unwrap();
            for i in 0..8u32 {
                let c = format!("record-{}", i).into_bytes();
                let h = hex::encode(digest_of(&c));
                s.put(&h, &c).unwrap();
                contents.push(c);
                hashes.push(h);
            }
            s.sync().unwrap();
        }
        let seg_dir = root.join("segments");
        // Segment 0 was sealed by rollover (2 records fit a 32-byte cap).
        let mut reg = PortalRegistry::new(&seg_dir);
        let entry = reg.register_segment("shard-0", 0, Some((0, 1)), ADDRESSING_CONTENT_DIGEST).unwrap();
        assert_eq!(entry.entries, 2);
        reg.save().unwrap();

        let reg2 = PortalRegistry::open(&seg_dir).unwrap();
        let portal = reg2.open_portal("shard-0").unwrap();
        for i in 0..2 {
            let d = digest_of(&contents[i]);
            assert_eq!(portal.read(&d).unwrap().unwrap(), contents[i]);
            assert_eq!(portal.read_hex(&hashes[i]).unwrap().unwrap(), contents[i]);
        }
        let absent = digest_of(b"never stored");
        assert_eq!(portal.lookup(&absent).unwrap(), None);
        assert_eq!(portal.read(&absent).unwrap(), None);
    }

    /// Write a segment + sorted .idx by hand (mirrors the on-disk formats).
    fn synth_shard(dir: &Path, id: u32, records: &[Vec<u8>]) -> Vec<[u8; 32]> {
        let seg_path = SegmentStore::seg_path(dir, id);
        let mut entries: Vec<(String, u64, u32)> = Vec::new();
        let mut digests = Vec::new();
        let mut f = File::create(&seg_path).unwrap();
        let mut off = 0u64;
        for c in records {
            let len = c.len() as u32;
            f.write_all(&len.to_le_bytes()).unwrap();
            f.write_all(c).unwrap();
            let d = digest_of(c);
            entries.push((hex::encode(d), off + 4, len));
            digests.push(d);
            off += 4 + len as u64;
        }
        drop(f);
        SegmentStore::write_idx(dir, id, &entries).unwrap(); // sorts at seal
        digests
    }

    #[test]
    fn tampered_record_is_rejected() {
        let dir = tempdir().unwrap();
        let digests = synth_shard(dir.path(), 0, &[b"authentic".to_vec()]);
        let seg_path = SegmentStore::seg_path(dir.path(), 0);
        let mut bytes = fs::read(&seg_path).unwrap();
        let n = bytes.len();
        bytes[n - 1] ^= 0xff;
        fs::write(&seg_path, bytes).unwrap();

        let mut reg = PortalRegistry::new(dir.path());
        reg.register_segment("p", 0, None, ADDRESSING_CONTENT_DIGEST).unwrap();
        let portal = reg.open_portal("p").unwrap();
        assert!(portal.read(&digests[0]).is_err(), "tampered bytes must not verify");
    }

    #[test]
    fn unsorted_idx_is_refused_at_registration() {
        let dir = tempdir().unwrap();
        let records: Vec<Vec<u8>> = (0..16u32).map(|i| format!("r-{}", i).into_bytes()).collect();
        synth_shard(dir.path(), 0, &records);
        // Reverse the entries inside the .idx (keep header + checksum valid
        // for the reversed body) — registration must still refuse: unsorted.
        let idx_path = SegmentStore::idx_path(dir.path(), 0);
        let data = fs::read(&idx_path).unwrap();
        let count = u64::from_le_bytes(data[4..12].try_into().unwrap()) as usize;
        let mut body = data[..IDX_HEADER_BYTES].to_vec();
        let mut chunks: Vec<&[u8]> = data[IDX_HEADER_BYTES..IDX_HEADER_BYTES + count * IDX_ENTRY_BYTES]
            .chunks(IDX_ENTRY_BYTES)
            .collect();
        chunks.reverse();
        for c in chunks {
            body.extend_from_slice(c);
        }
        let mut out = body.clone();
        out.extend_from_slice(&digest_of(&body));
        fs::write(&idx_path, out).unwrap();

        let mut reg = PortalRegistry::new(dir.path());
        assert!(reg.register_segment("p", 0, None, ADDRESSING_CONTENT_DIGEST).is_err());
    }

    /// Backcompat: the SAME sealed shard must answer identically through
    /// the legacy SegmentStore path (index loaded into memory) and through
    /// a portal (index searched on disk). Present records byte-identical,
    /// absent records None in both.
    #[test]
    fn legacy_segment_reads_match_portal_reads() {
        let dir = tempdir().unwrap();
        let root = dir.path().to_path_buf();
        let mut hashes: Vec<String> = Vec::new();
        let mut contents: Vec<Vec<u8>> = Vec::new();
        {
            let s = SegmentStore::open_with_max(&root, 64).unwrap();
            for i in 0..12u32 {
                let c = format!("legacy-record-{}", i).into_bytes();
                let h = hex::encode(digest_of(&c));
                s.put(&h, &c).unwrap();
                hashes.push(h);
                contents.push(c);
            }
            s.sync().unwrap();
        }
        // Legacy answers (fresh SegmentStore open, in-memory index).
        let legacy = SegmentStore::open_with_max(&root, 64).unwrap();
        let seg_dir = root.join("segments");
        // Portal answers for the sealed segments only (0..=last-1 are
        // sealed; the final segment is still active). Collect every sealed
        // segment id that has an .idx.
        let mut reg = PortalRegistry::new(&seg_dir);
        let mut sealed: Vec<u32> = Vec::new();
        for id in 0..12u32 {
            if SegmentStore::idx_path(&seg_dir, id).exists()
                && SegmentStore::seg_path(&seg_dir, id).exists()
            {
                // Stop at the active segment: it has no idx by design.
                sealed.push(id);
            }
        }
        assert!(sealed.len() >= 2, "expected at least 2 sealed segments, got {:?}", sealed);
        for (n, id) in sealed.iter().enumerate() {
            reg.register_segment(&format!("sealed-{}", n), *id, None, ADDRESSING_CONTENT_DIGEST)
                .unwrap();
        }
        reg.save().unwrap();
        let reg2 = PortalRegistry::open(&seg_dir).unwrap();
        let portals: Vec<Portal> = sealed
            .iter()
            .enumerate()
            .map(|(n, _)| reg2.open_portal(&format!("sealed-{}", n)).unwrap())
            .collect();
        let mut portal_hits = 0usize;
        for (h, c) in hashes.iter().zip(contents.iter()) {
            let want = legacy.get(h).unwrap();
            let mut got: Option<Vec<u8>> = None;
            for p in &portals {
                if let Some(v) = p.read_hex(h).unwrap() {
                    got = Some(v);
                    break;
                }
            }
            match (&want, &got) {
                (Some(w), Some(g)) => {
                    assert_eq!(w, g);
                    assert_eq!(g, c);
                    portal_hits += 1;
                }
                (Some(_), None) => {
                    // Record lives in the still-active segment: legacy can
                    // read it, no portal serves it yet. Expected, counted
                    // below by the sealed quota.
                }
                _ => panic!("legacy/portal disagree for {}", h),
            }
        }
        assert!(portal_hits >= 6, "only {} sealed records read via portals", portal_hits);
        let absent = hex::encode(digest_of(b"legacy-absent"));
        assert_eq!(legacy.get(&absent).unwrap(), None);
        for p in &portals {
            assert_eq!(p.read_hex(&absent).unwrap(), None);
        }
    }

    /// Cross-core conformance, direction 1: a fixture shard written by the
    /// PYTHON engine (portal.py `write_content_shard` + registry) must read
    /// identically through this Rust portal. Ignored unless
    /// NEDB_PORTAL_FIXTURE_DIR names a fixture dir holding portals.json,
    /// seg-000000.dat/.idx and expected.json:
    ///   [{"digest": hex, "content_hex": hex}, ...]; an absent record is an
    ///   entry whose content_hex is null.
    #[test]
    #[ignore]
    fn cross_core_fixture_written_by_python() {
        let dir = std::env::var("NEDB_PORTAL_FIXTURE_DIR")
            .expect("set NEDB_PORTAL_FIXTURE_DIR to a Python-written fixture");
        let root = PathBuf::from(&dir);
        let expected_raw = fs::read(root.join("expected.json")).unwrap();
        let expected: Vec<serde_json::Value> =
            serde_json::from_slice(&expected_raw).unwrap();
        let reg = PortalRegistry::open(&root).unwrap();
        let portal = reg.open_portal("fixture").unwrap();
        let mut checked = 0usize;
        for item in &expected {
            let digest = item["digest"].as_str().unwrap();
            match item["content_hex"].as_str() {
                Some(c) => {
                    let want = hex::decode(c).unwrap();
                    let got = portal.read_hex(digest).unwrap().expect("present");
                    assert_eq!(got, want, "digest {} mismatch", digest);
                }
                None => {
                    assert_eq!(portal.read_hex(digest).unwrap(), None);
                }
            }
            checked += 1;
        }
        assert!(checked > 0);
    }

    /// Cross-core conformance, direction 2: write a fixture shard + registry
    /// + expected.json from RUST, for the Python checker
    /// (tools/portal_fixture_check.py in the repo root, or any portal.py
    /// reader) to verify. Ignored unless NEDB_PORTAL_FIXTURE_OUT is set.
    #[test]
    #[ignore]
    fn cross_core_fixture_written_by_rust() {
        let out = std::env::var("NEDB_PORTAL_FIXTURE_OUT")
            .expect("set NEDB_PORTAL_FIXTURE_OUT to a target fixture dir");
        let root = PathBuf::from(&out);
        fs::create_dir_all(&root).unwrap();
        let records: Vec<Vec<u8>> =
            (0..500u32).map(|i| format!("dag-node-{}", i).into_bytes()).collect();
        let digests = synth_shard(&root, 0, &records);
        let mut reg = PortalRegistry::new(&root);
        reg.register_segment("fixture", 0, None, ADDRESSING_CONTENT_DIGEST).unwrap();
        reg.save().unwrap();
        let mut expected: Vec<serde_json::Value> = Vec::new();
        for (d, c) in digests.iter().zip(records.iter()) {
            expected.push(serde_json::json!({
                "digest": hex::encode(d),
                "content_hex": hex::encode(c),
            }));
        }
        expected.push(serde_json::json!({
            "digest": hex::encode(digest_of(b"never-written-node")),
            "content_hex": null,
        }));
        fs::write(
            root.join("expected.json"),
            serde_json::to_string(&expected).unwrap(),
        )
        .unwrap();
    }

    /// 100k-entry shard: lookup must stay O(log n) in idx reads (~17), not
    /// O(n), and reads must return the exact record bytes.
    #[test]
    fn large_idx_lookup_is_log_n_reads() {
        let dir = tempdir().unwrap();
        const N: u32 = 100_000;
        let records: Vec<Vec<u8>> =
            (0..N).map(|i| format!("rec-{:06}", i).into_bytes()).collect();
        let digests = synth_shard(dir.path(), 0, &records);

        let mut reg = PortalRegistry::new(dir.path());
        let entry = reg.register_segment("big", 0, None, ADDRESSING_CONTENT_DIGEST).unwrap();
        assert_eq!(entry.entries, N as u64);
        let portal = reg.open_portal("big").unwrap();

        for &i in &[0usize, 1, 49_999, 99_999] {
            let before = portal.idx_reads();
            let got = portal.read(&digests[i]).unwrap().unwrap();
            assert_eq!(got, records[i]);
            let reads = portal.idx_reads() - before;
            assert!(reads <= 18, "lookup used {} idx reads for 100k entries (want <= ~17)", reads);
        }
        let before = portal.idx_reads();
        assert_eq!(portal.read(&digest_of(b"absent")).unwrap(), None);
        assert!(portal.idx_reads() - before <= 18);
    }
}
