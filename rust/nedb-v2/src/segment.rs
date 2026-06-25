//! segment.rs — NEDB v3 packed object substrate.
//!
//! v3 keeps the v2 logical model intact (content-addressed, immutable,
//! BLAKE2b-verified DAG nodes) and changes only *where the bytes live*: instead
//! of one filesystem object per node — which caps throughput at the OS
//! small-file metadata rate — many immutable objects are appended into
//! **segment files** addressed through an in-memory `hash -> (segment, offset,
//! len)` index. The hash is still `BLAKE2b(content)` and is re-verified on every
//! read, so content-addressing and tamper-evidence are unchanged.
//!
//! This module knows nothing about `Node`, JSON, or encryption: callers
//! (`ObjectStore`) pass already-serialized/encrypted `content` bytes and the
//! precomputed hash. That keeps the segment store a pure content<->location
//! layer and leaves all crypto/serialization in `store.rs`.
//!
//! Opt-in only: `ObjectStore` instantiates this when `NEDB_DAG_V3` is set
//! (surfaced as the `--dag-v3` flag). Default storage is byte-for-byte v2.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use anyhow::{bail, Context, Result};
use blake2::{Blake2b512, Digest};
use dashmap::DashMap;

/// Default segment rollover size (256 MiB).
const DEFAULT_MAX_SEGMENT_BYTES: u64 = 256 * 1024 * 1024;

/// Location of one content record inside the segment set.
#[derive(Clone, Copy, Debug)]
struct SegmentLocation {
    segment_id: u32,
    /// Byte offset of the CONTENT (immediately after the u32 length prefix).
    offset: u64,
    len: u32,
}

/// BLAKE2b-256 (first 32 bytes of Blake2b-512), hex-encoded.
/// MUST match `store::blake2b` so segment hashes equal loose-object hashes.
fn blake2b(data: &[u8]) -> String {
    let mut h = Blake2b512::new();
    h.update(data);
    hex::encode(&h.finalize()[..32])
}

/// The currently-appended-to segment.
struct Active {
    id: u32,
    file: File,
    /// End-of-file = next append position (kept in sync with the file cursor).
    offset: u64,
}

/// Append-only, content-addressed packed object store with an in-memory index.
pub struct SegmentStore {
    dir: PathBuf,
    index: DashMap<String, SegmentLocation>,
    active: Mutex<Active>,
    max_segment_bytes: u64,
}

impl SegmentStore {
    fn seg_path(dir: &Path, id: u32) -> PathBuf {
        dir.join(format!("seg-{:06}.dat", id))
    }

    /// Open (or create) the segment store under `{objects_root}/segments`,
    /// rebuilding the index by scanning every segment in ascending order and
    /// truncating any torn tail on the active (highest-id) segment.
    pub fn open(objects_root: &Path) -> Result<Self> {
        Self::open_with_max(objects_root, DEFAULT_MAX_SEGMENT_BYTES)
    }

    /// Like `open`, with an explicit rollover size (used by tests).
    pub fn open_with_max(objects_root: &Path, max_segment_bytes: u64) -> Result<Self> {
        let dir = objects_root.join("segments");
        fs::create_dir_all(&dir).context("create objects/segments dir")?;

        // Discover existing segment ids.
        let mut ids: Vec<u32> = Vec::new();
        for entry in fs::read_dir(&dir).context("read segments dir")? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().to_string();
            if let Some(rest) = name.strip_prefix("seg-") {
                if let Some(num) = rest.strip_suffix(".dat") {
                    if let Ok(id) = num.parse::<u32>() {
                        ids.push(id);
                    }
                }
            }
        }
        ids.sort_unstable();

        let index: DashMap<String, SegmentLocation> = DashMap::new();
        let mut active_id: u32 = 0;
        let mut active_end: u64 = 0;

        for (pos, &id) in ids.iter().enumerate() {
            let valid_end = Self::scan_segment(&dir, id, &index)?;
            if pos + 1 == ids.len() {
                // Active (last) segment: truncate any torn tail from a crash.
                let path = Self::seg_path(&dir, id);
                let file_len = fs::metadata(&path)?.len();
                if valid_end < file_len {
                    let f = OpenOptions::new().write(true).open(&path)?;
                    f.set_len(valid_end)?;
                }
                active_id = id;
                active_end = valid_end;
            }
        }

        // Open (creating if necessary) the active segment for appending.
        let active_path = Self::seg_path(&dir, active_id);
        let mut file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&active_path)
            .with_context(|| format!("open active segment {:?}", active_path))?;
        file.seek(SeekFrom::Start(active_end))?;

        Ok(Self {
            dir,
            index,
            active: Mutex::new(Active { id: active_id, file, offset: active_end }),
            max_segment_bytes,
        })
    }

    /// Scan one segment, inserting every complete record into `index`.
    /// Returns the byte offset of the first incomplete (torn) record — i.e. the
    /// valid length of the segment.
    fn scan_segment(dir: &Path, id: u32, index: &DashMap<String, SegmentLocation>) -> Result<u64> {
        let path = Self::seg_path(dir, id);
        let mut f = match File::open(&path) {
            Ok(f) => f,
            Err(_) => return Ok(0),
        };
        let file_len = f.metadata()?.len();
        let mut pos: u64 = 0;
        loop {
            if pos + 4 > file_len {
                break; // no room for a length prefix → torn tail
            }
            f.seek(SeekFrom::Start(pos))?;
            let mut len_buf = [0u8; 4];
            if f.read_exact(&mut len_buf).is_err() {
                break;
            }
            let len = u32::from_le_bytes(len_buf);
            let content_off = pos + 4;
            if content_off + (len as u64) > file_len {
                break; // declared length overruns EOF → torn content
            }
            let mut content = vec![0u8; len as usize];
            if f.read_exact(&mut content).is_err() {
                break;
            }
            let hash = blake2b(&content);
            index.insert(hash, SegmentLocation { segment_id: id, offset: content_off, len });
            pos = content_off + len as u64;
        }
        Ok(pos)
    }

    /// True if this hash is already stored in a segment.
    pub fn contains(&self, hash: &str) -> bool {
        self.index.contains_key(hash)
    }

    /// Append `content` under `hash` (idempotent). `hash` must equal
    /// `BLAKE2b(content)`; the caller computes it (parallel, outside the lock).
    pub fn put(&self, hash: &str, content: &[u8]) -> Result<()> {
        if self.index.contains_key(hash) {
            return Ok(()); // already stored — content-addressed, so identical
        }

        let len = content.len() as u32;
        let record_size = 4u64 + content.len() as u64;

        let mut active = self.active.lock().unwrap();

        // Re-check under the lock (another thread may have just written it).
        if self.index.contains_key(hash) {
            return Ok(());
        }

        // Roll over if this record would push the segment past the cap (but
        // always allow at least one record so an oversized record still lands).
        if active.offset > 0 && active.offset + record_size > self.max_segment_bytes {
            let _ = active.file.flush();
            let _ = active.file.sync_all();
            let next_id = active.id + 1;
            let path = Self::seg_path(&self.dir, next_id);
            let file = OpenOptions::new()
                .create(true)
                .read(true)
                .write(true)
                .open(&path)
                .with_context(|| format!("open new segment {:?}", path))?;
            *active = Active { id: next_id, file, offset: 0 };
        }

        let content_off = active.offset + 4;
        // One sequential write of [len][content]; the cursor is already at the
        // end, so no seek is needed. No per-object fsync (deferred to sync()).
        let mut rec = Vec::with_capacity(4 + content.len());
        rec.extend_from_slice(&len.to_le_bytes());
        rec.extend_from_slice(content);
        active.file.write_all(&rec)?;

        let seg_id = active.id;
        active.offset += record_size;
        self.index.insert(
            hash.to_string(),
            SegmentLocation { segment_id: seg_id, offset: content_off, len },
        );
        Ok(())
    }

    /// Read the raw content bytes for `hash`, or `None` if not stored in any
    /// segment (caller then falls back to the loose-object path). Re-verifies
    /// `BLAKE2b(content) == hash` (tamper-evidence; never returns bad data).
    pub fn get(&self, hash: &str) -> Result<Option<Vec<u8>>> {
        let loc = match self.index.get(hash) {
            Some(entry) => *entry.value(),
            None => return Ok(None),
        };
        let path = Self::seg_path(&self.dir, loc.segment_id);
        let mut f = File::open(&path).with_context(|| format!("open segment {:?}", path))?;
        f.seek(SeekFrom::Start(loc.offset))?;
        let mut content = vec![0u8; loc.len as usize];
        f.read_exact(&mut content)
            .with_context(|| format!("read record from segment {}", loc.segment_id))?;
        let actual = blake2b(&content);
        if actual != hash {
            bail!("segment object {} tampered: recomputed {}", hash, actual);
        }
        Ok(Some(content))
    }

    /// All hashes currently stored in segments.
    pub fn all_hashes(&self) -> Vec<String> {
        self.index.iter().map(|e| e.key().clone()).collect()
    }

    /// Flush + fsync the active segment. One durability point per batch
    /// (wired into `Db::flush_all`).
    pub fn sync(&self) -> Result<()> {
        let mut active = self.active.lock().unwrap();
        let _ = active.file.flush();
        active.file.sync_all().context("fsync active segment")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn put_get_hash(s: &SegmentStore, content: &[u8]) -> String {
        let h = blake2b(content);
        s.put(&h, content).unwrap();
        h
    }

    #[test]
    fn put_get_roundtrip() {
        let dir = tempdir().unwrap();
        let s = SegmentStore::open(dir.path()).unwrap();
        let h = put_get_hash(&s, b"hello nedb v3");
        assert_eq!(s.get(&h).unwrap().unwrap(), b"hello nedb v3");
        assert!(s.contains(&h));
        // unknown hash → None (caller falls back to loose objects)
        assert!(s.get(&"0".repeat(64)).unwrap().is_none());
    }

    #[test]
    fn idempotent_put() {
        let dir = tempdir().unwrap();
        let s = SegmentStore::open(dir.path()).unwrap();
        let h1 = put_get_hash(&s, b"dup");
        let h2 = put_get_hash(&s, b"dup");
        assert_eq!(h1, h2);
        assert_eq!(s.all_hashes().len(), 1);
    }

    #[test]
    fn index_rebuilt_on_reopen() {
        let dir = tempdir().unwrap();
        let h = {
            let s = SegmentStore::open(dir.path()).unwrap();
            let h = put_get_hash(&s, b"persisted");
            s.sync().unwrap();
            h
        };
        // Reopen: index must be rebuilt by scanning the segment.
        let s2 = SegmentStore::open(dir.path()).unwrap();
        assert_eq!(s2.get(&h).unwrap().unwrap(), b"persisted");
    }

    #[test]
    fn rollover_creates_new_segments() {
        let dir = tempdir().unwrap();
        // Tiny cap forces a new segment almost every record.
        let s = SegmentStore::open_with_max(dir.path(), 32).unwrap();
        let mut hashes = Vec::new();
        for i in 0..8u32 {
            hashes.push(put_get_hash(&s, format!("record-{}", i).as_bytes()));
        }
        s.sync().unwrap();
        for h in &hashes {
            assert!(s.get(h).unwrap().is_some());
        }
        // More than one segment file should exist.
        let seg_files = fs::read_dir(dir.path().join("segments")).unwrap().count();
        assert!(seg_files > 1, "expected rollover into multiple segments");
    }

    #[test]
    fn torn_tail_is_truncated_on_open() {
        let dir = tempdir().unwrap();
        let good = {
            let s = SegmentStore::open(dir.path()).unwrap();
            let h = put_get_hash(&s, b"good record");
            s.sync().unwrap();
            h
        };
        // Append a torn record: a length prefix claiming more bytes than follow.
        // open() roots segments at {objects_root}/segments, and tests pass the
        // tempdir as objects_root, so the active segment is {dir}/segments/seg-000000.dat.
        let seg = dir.path().join("segments").join("seg-000000.dat");
        {
            let mut f = OpenOptions::new().append(true).open(&seg).unwrap();
            f.write_all(&9999u32.to_le_bytes()).unwrap(); // claims 9999 bytes
            f.write_all(b"short").unwrap(); // only 5 follow → torn
        }
        // Reopen: the torn tail must be truncated, the good record preserved.
        let s2 = SegmentStore::open(dir.path()).unwrap();
        assert_eq!(s2.get(&good).unwrap().unwrap(), b"good record");
        // A fresh write must still succeed after truncation.
        let h2 = put_get_hash(&s2, b"after recovery");
        assert!(s2.get(&h2).unwrap().is_some());
    }

    #[test]
    fn tamper_detected_on_read() {
        let dir = tempdir().unwrap();
        let h = {
            let s = SegmentStore::open(dir.path()).unwrap();
            let h = put_get_hash(&s, b"authentic");
            s.sync().unwrap();
            h
        };
        // Corrupt the content byte in the segment file.
        let seg = dir.path().join("segments").join("seg-000000.dat");
        let mut bytes = fs::read(&seg).unwrap();
        let n = bytes.len();
        bytes[n - 1] ^= 0xff;
        fs::write(&seg, bytes).unwrap();
        let s2 = SegmentStore::open(dir.path()).unwrap();
        // The corrupted record now hashes to a different address, so the
        // original hash is simply not found (and any direct read verifies).
        match s2.get(&h) {
            Ok(None) => {}            // re-indexed under a different hash
            Err(_) => {}              // or a direct verify failure
            Ok(Some(_)) => panic!("tampered content must not verify under original hash"),
        }
    }
}
