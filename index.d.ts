// nedb-engine — public type surface.
//
// Runtime behavior (durable-mode auto-flush-on-exit) is added by the wrapper in
// index.js; the type surface is exactly the generated native binding's.
export * from './native';

// ── wrap adapter family (wrap/*.js) ─────────────────────────────────────────

/** Options accepted by every wrap_* constructor. */
export interface WrapOptions {
  /** Logical database name (default "default"). */
  dbName?: string;
  /** HTTP nedbd server (v1 AOF, `--dag` v2, `--dag-v3` v3). Overrides embedded DAG. */
  nedbdUrl?: string;
  /** Bearer token for nedbd (NEDBD_TOKEN on the server). */
  nedbdToken?: string;
  /** Durable DAG store directory (embedded mode). */
  dagPath?: string;
  /** 64-hex TMK → AES-256-GCM at-rest encryption (embedded DAG mode). */
  dagTmk?: string;
  /** Explicit NedbCore class (testing / custom builds). */
  native?: unknown;
}

/** The `.nedb` attribute — full NEDB layer-2 API. */
export interface NedbSurface {
  register(pattern: string, collection: string, opts?: {
    idExtractor?: (key: string) => string;
    valueParser?: (raw: unknown) => Record<string, unknown>;
    valueType?: 'string' | 'hash' | 'json';
  }): NedbSurface;
  backfill(opts?: { pattern?: string; collection?: string; batchSize?: number }): number;
  shadowWrites: boolean;
  readonly engineKind: 'dag-embedded' | 'nedbd-http' | 'aof-embedded';

  put(coll: string, id: string, doc: Record<string, unknown>): Record<string, unknown>;
  get(coll: string, id: string, asOf?: number): Record<string, unknown> | null;
  query(nql: string): Array<Record<string, unknown>>;
  createIndex(coll: string, field: string, kind?: string): void;
  delete(coll: string, id: string): void;
  link(frm: string, rel: string, to: string): void;
  unlink(frm: string, rel: string, to: string): void;
  neighbors(frm: string, rel: string, asOf?: number): string[];
  inbound(to: string, rel: string, asOf?: number): string[];
  verify(): boolean;
  readonly head: string;
  readonly seq: number;
  checkpoint(): string;
  /** DAG-native: latest node (null on non-DAG backends). */
  tip(): Record<string, unknown> | null;
  /** DAG-native: changefeed page, after_seq exclusive. */
  since(afterSeq: number | bigint, limit?: number): {
    nodes: Array<Record<string, unknown>>; from_seq: number; to_seq: number;
    head_seq: number; has_more: boolean;
  };
  /** DAG-native: replication readiness. */
  scanStatus(): { scan_complete: boolean; tip_seq: number; indexed_count: number; [k: string]: unknown };
}

export declare function wrapRedis<T extends object = object>(client: T, opts?: WrapOptions): T & { nedb: NedbSurface };
export declare function wrapSqlite<T extends object = object>(conn: T, opts?: WrapOptions): T & { nedb: NedbSurface };
export declare function wrapMysql<T extends object = object>(conn: T, opts?: WrapOptions): T & { nedb: NedbSurface };
export declare function wrapPg<T extends object = object>(conn: T, opts?: WrapOptions): T & { nedb: NedbSurface };
export declare function wrapMongo<T extends object = object>(client: T, opts?: WrapOptions): T & { nedb: NedbSurface };
