"""AES-GCM in the standard library alone, for sealing the published site.

AES (FIPS 197) with 128-, 192- or 256-bit keys, GCM (NIST SP 800-38D) with a 96-bit nonce and a 128-bit tag,
the same construction the browser's WebCrypto decrypts with {name: "AES-GCM"}. Encryption only needs the forward
cipher, so there is no inverse cipher here: GCM decrypts with the same counter stream. The S-box is generated,
not typed, and tests/test_lock.py checks the FIPS 197 known answers and the GCM specification's test cases.

Speed: byte tables for the rounds (the classic T-tables) and 16 byte tables for the GHASH multiplication; about
a quarter megabyte a second of ciphertext on a laptop, which is seconds for the site bundle. Not constant time:
this runs on the publisher's own machine over data it is about to make public in sealed form, never on a server
answering strangers.
"""

import struct

__all__ = ["encrypt", "decrypt", "AuthenticationError", "aes_encrypt_block"]


class AuthenticationError(ValueError):
    """The tag did not match: wrong key, wrong nonce, wrong additional data or a changed ciphertext."""


# ------------------------------------------------------------------------------------------- the S-box, generated
def _sbox():
    s = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)         # p *= 3 in GF(2^8)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09                                                 # q /= 3
        x = q ^ (q << 1) ^ (q << 2) ^ (q << 3) ^ (q << 4)
        s[p] = (x ^ (x >> 8) ^ 0x63) & 0xFF
        if p == 1:
            break
    s[0] = 0x63
    return s


SBOX = _sbox()


def _xtime(b):
    b <<= 1
    return (b ^ 0x1B) & 0xFF if b & 0x100 else b


def _tables():
    """Te0..Te3: SubBytes, ShiftRows and MixColumns folded into four 32-bit lookups per column."""
    te0, te1, te2, te3 = [], [], [], []
    for x in range(256):
        s = SBOX[x]
        s2 = _xtime(s)
        s3 = s2 ^ s
        w = (s2 << 24) | (s << 16) | (s << 8) | s3
        te0.append(w)
        te1.append(((w >> 8) | (w << 24)) & 0xFFFFFFFF)
        te2.append(((w >> 16) | (w << 16)) & 0xFFFFFFFF)
        te3.append(((w >> 24) | (w << 8)) & 0xFFFFFFFF)
    return te0, te1, te2, te3


TE0, TE1, TE2, TE3 = _tables()
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


# ------------------------------------------------------------------------------------------- key schedule, cipher
def expand_key(key):
    """The round keys as a flat list of 32-bit words, 4 per round."""
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must be 16, 24 or 32 bytes")
    nk = len(key) // 4
    nr = nk + 6
    w = list(struct.unpack(">%dI" % nk, key))
    for i in range(nk, 4 * (nr + 1)):
        t = w[i - 1]
        if i % nk == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF                   # RotWord
            t = (SBOX[t >> 24] << 24) | (SBOX[(t >> 16) & 0xFF] << 16) | (SBOX[(t >> 8) & 0xFF] << 8) | SBOX[t & 0xFF]
            t ^= RCON[i // nk - 1] << 24
        elif nk > 6 and i % nk == 4:
            t = (SBOX[t >> 24] << 24) | (SBOX[(t >> 16) & 0xFF] << 16) | (SBOX[(t >> 8) & 0xFF] << 8) | SBOX[t & 0xFF]
        w.append(w[i - nk] ^ t)
    return w, nr


def _encrypt_words(rk, nr, s0, s1, s2, s3):
    """One block, the state as four big-endian column words. Returns the four output words."""
    s0 ^= rk[0]
    s1 ^= rk[1]
    s2 ^= rk[2]
    s3 ^= rk[3]
    k = 4
    for _ in range(nr - 1):
        t0 = TE0[s0 >> 24] ^ TE1[(s1 >> 16) & 0xFF] ^ TE2[(s2 >> 8) & 0xFF] ^ TE3[s3 & 0xFF] ^ rk[k]
        t1 = TE0[s1 >> 24] ^ TE1[(s2 >> 16) & 0xFF] ^ TE2[(s3 >> 8) & 0xFF] ^ TE3[s0 & 0xFF] ^ rk[k + 1]
        t2 = TE0[s2 >> 24] ^ TE1[(s3 >> 16) & 0xFF] ^ TE2[(s0 >> 8) & 0xFF] ^ TE3[s1 & 0xFF] ^ rk[k + 2]
        t3 = TE0[s3 >> 24] ^ TE1[(s0 >> 16) & 0xFF] ^ TE2[(s1 >> 8) & 0xFF] ^ TE3[s2 & 0xFF] ^ rk[k + 3]
        s0, s1, s2, s3 = t0, t1, t2, t3
        k += 4
    sb = SBOX
    o0 = ((sb[s0 >> 24] << 24) | (sb[(s1 >> 16) & 0xFF] << 16) | (sb[(s2 >> 8) & 0xFF] << 8) | sb[s3 & 0xFF]) ^ rk[k]
    o1 = ((sb[s1 >> 24] << 24) | (sb[(s2 >> 16) & 0xFF] << 16) | (sb[(s3 >> 8) & 0xFF] << 8) | sb[s0 & 0xFF]) ^ rk[k + 1]
    o2 = ((sb[s2 >> 24] << 24) | (sb[(s3 >> 16) & 0xFF] << 16) | (sb[(s0 >> 8) & 0xFF] << 8) | sb[s1 & 0xFF]) ^ rk[k + 2]
    o3 = ((sb[s3 >> 24] << 24) | (sb[(s0 >> 16) & 0xFF] << 16) | (sb[(s1 >> 8) & 0xFF] << 8) | sb[s2 & 0xFF]) ^ rk[k + 3]
    return o0, o1, o2, o3


def aes_encrypt_block(key, block):
    """The raw forward cipher on one 16-byte block (the FIPS 197 known answers test this)."""
    if len(block) != 16:
        raise ValueError("a block is 16 bytes")
    rk, nr = expand_key(key)
    return struct.pack(">4I", *_encrypt_words(rk, nr, *struct.unpack(">4I", block)))


# ------------------------------------------------------------------------------------------- GHASH
_R = 0xE1 << 120


def _gf_mult(x, y):
    """Multiplication in GF(2^128) with GCM's bit order (Algorithm 1 of SP 800-38D)."""
    z = 0
    v = x
    for i in range(127, -1, -1):
        if (y >> i) & 1:
            z ^= v
        v = (v >> 1) ^ _R if v & 1 else v >> 1
    return z


def _ghash_tables(h):
    """16 tables of 256 entries: M[i][b] = (b placed in byte i) * H, so a block product is 16 lookups."""
    tables = []
    for i in range(16):
        shift = 8 * (15 - i)
        bits = [_gf_mult(1 << (shift + k), h) for k in range(8)]
        t = [0] * 256
        for b in range(1, 256):
            low = b & -b                                              # lowest set bit
            t[b] = t[b ^ low] ^ bits[low.bit_length() - 1]
        tables.append(t)
    return tables


def _ghash(tables, data):
    """GHASH over data already padded to a multiple of 16 bytes."""
    y = 0
    for off in range(0, len(data), 16):
        x = y ^ int.from_bytes(data[off:off + 16], "big")
        acc = 0
        for i in range(16):
            acc ^= tables[i][(x >> (8 * (15 - i))) & 0xFF]
        y = acc
    return y


def _pad16(b):
    r = len(b) % 16
    return b if r == 0 else b + bytes(16 - r)


# ------------------------------------------------------------------------------------------- GCM
def _ctr_stream(rk, nr, j0, nbytes):
    """The counter stream from inc32(J0), nbytes long."""
    n0, n1, n2, c = struct.unpack(">4I", j0)
    out = bytearray()
    blocks = (nbytes + 15) // 16
    for _ in range(blocks):
        c = (c + 1) & 0xFFFFFFFF
        out += struct.pack(">4I", *_encrypt_words(rk, nr, n0, n1, n2, c))
    return bytes(out[:nbytes])


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b)) if len(a) < 64 else (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _setup(key, nonce):
    if len(nonce) != 12:
        raise ValueError("GCM nonce must be 12 bytes here (the browser side uses the same)")
    rk, nr = expand_key(key)
    h = int.from_bytes(struct.pack(">4I", *_encrypt_words(rk, nr, 0, 0, 0, 0)), "big")
    j0 = bytes(nonce) + b"\x00\x00\x00\x01"
    return rk, nr, h, j0


def _tag(rk, nr, h, j0, aad, ciphertext):
    tables = _ghash_tables(h)
    lengths = struct.pack(">QQ", 8 * len(aad), 8 * len(ciphertext))
    s = _ghash(tables, _pad16(bytes(aad)) + _pad16(ciphertext) + lengths)
    ekj0 = struct.pack(">4I", *_encrypt_words(rk, nr, *struct.unpack(">4I", j0)))
    return _xor(s.to_bytes(16, "big"), ekj0)


def encrypt(key, nonce, plaintext, aad=b""):
    """ciphertext || 16-byte tag, as WebCrypto's AES-GCM produces and expects."""
    rk, nr, h, j0 = _setup(key, nonce)
    plaintext = bytes(plaintext)
    ciphertext = _xor(plaintext, _ctr_stream(rk, nr, j0, len(plaintext))) if plaintext else b""
    return ciphertext + _tag(rk, nr, h, j0, aad, ciphertext)


def decrypt(key, nonce, data, aad=b""):
    """The plaintext, or AuthenticationError. The tag is checked before anything is returned."""
    data = bytes(data)
    if len(data) < 16:
        raise AuthenticationError("too short to carry a tag")
    rk, nr, h, j0 = _setup(key, nonce)
    ciphertext, tag = data[:-16], data[-16:]
    want = _tag(rk, nr, h, j0, aad, ciphertext)
    diff = 0
    for a, b in zip(want, tag):
        diff |= a ^ b
    if diff:
        raise AuthenticationError("authentication failed")
    return _xor(ciphertext, _ctr_stream(rk, nr, j0, len(ciphertext))) if ciphertext else b""
