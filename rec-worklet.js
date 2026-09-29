// Captures microphone audio as 16 kHz mono 16-bit PCM.
// Input is low-pass filtered upstream (two biquads at 7 kHz), then resampled here
// by linear interpolation from the context rate (usually 48 kHz on phones).
// Posts {pcm: Int16Array, rms: Number} about every 0.5 s, and a final partial block on 'flush'.
const OUT_RATE = 16000;
const BLOCK = 8000; // 0.5 s at 16 kHz

class PCM16kCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / OUT_RATE; // input samples per output sample
    this.t = 0;                        // fractional read position within the current input block
    this.prev = 0;                     // last input sample of the previous block (index -1)
    this.buf = new Int16Array(BLOCK);
    this.n = 0;
    this.sq = 0;
    this.port.onmessage = e => { if (e.data === 'flush') this.post(true); };
  }

  post(final) {
    const pcm = this.buf.slice(0, this.n);
    const rms = this.n ? Math.sqrt(this.sq / this.n) : 0;
    this.port.postMessage({ pcm, rms, final: !!final }, [pcm.buffer]);
    this.n = 0; this.sq = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !ch.length) return true;
    const len = ch.length;
    let t = this.t;
    while (t < len - 1) {
      const i = Math.floor(t);
      const f = t - i;
      const s0 = i < 0 ? this.prev : ch[i];
      const s1 = ch[i + 1];
      let v = s0 + (s1 - s0) * f;
      if (v > 1) v = 1; else if (v < -1) v = -1;
      this.sq += v * v;
      this.buf[this.n++] = v < 0 ? v * 32768 : v * 32767;
      if (this.n === BLOCK) this.post(false);
      t += this.step;
    }
    this.t = t - len;
    this.prev = ch[len - 1];
    return true;
  }
}

registerProcessor('pcm16k-capture', PCM16kCapture);
