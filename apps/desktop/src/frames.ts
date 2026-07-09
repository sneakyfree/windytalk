// voice-session.v1 §2 binary frame codec — the client mirror of the engine's
// struct "<BBHQI": u8 type, u8 flags, u16 seq, u64 ts_ms, u32 stream_id, then payload.
// All multi-byte fields little-endian (§1). 16-byte header.

export const MIC_TYPE = 0x01;
export const TTS_TYPE = 0x02;
export const FLAG_FINAL = 0x01;
export const HEADER_LEN = 16;

export interface Frame {
  type: number;
  flags: number;
  seq: number;
  tsMs: number;
  streamId: number;
  payload: Uint8Array;
}

export function buildFrame(
  type: number,
  flags: number,
  seq: number,
  tsMs: number,
  streamId: number,
  payload: Uint8Array,
): ArrayBuffer {
  const buf = new ArrayBuffer(HEADER_LEN + payload.length);
  const view = new DataView(buf);
  view.setUint8(0, type);
  view.setUint8(1, flags);
  view.setUint16(2, seq & 0xffff, true);
  view.setBigUint64(4, BigInt(Math.max(0, Math.floor(tsMs))), true);
  view.setUint32(12, streamId >>> 0, true);
  new Uint8Array(buf, HEADER_LEN).set(payload);
  return buf;
}

export function parseFrame(buf: ArrayBuffer): Frame | null {
  if (buf.byteLength < HEADER_LEN) return null;
  const view = new DataView(buf);
  return {
    type: view.getUint8(0),
    flags: view.getUint8(1),
    seq: view.getUint16(2, true),
    tsMs: Number(view.getBigUint64(4, true)),
    streamId: view.getUint32(12, true),
    payload: new Uint8Array(buf.slice(HEADER_LEN)),
  };
}
