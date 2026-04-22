/*
 * Minimal USTAR / GNU tar reader.
 *
 * Exposes a single global ``window.Untar`` with one function:
 *
 *     Untar.parse(uint8Array) -> Array<{name, data: Uint8Array}>
 *
 * Only regular files are returned; directories and other typeflags are
 * skipped but their (zero-length) data sections are walked so the cursor
 * stays aligned.  GNU long-name extensions (typeflag 'L') are honoured.
 *
 * License: MIT (hand-rolled for Agora CMS).
 */
(function (root) {
    "use strict";

    const BLOCK = 512;

    function parseOctal(bytes) {
        // Strip trailing NUL / space bytes, then parse as octal.
        let end = bytes.length;
        while (end > 0 && (bytes[end - 1] === 0 || bytes[end - 1] === 0x20)) {
            end -= 1;
        }
        if (end === 0) return 0;
        let s = "";
        for (let i = 0; i < end; i++) s += String.fromCharCode(bytes[i]);
        const n = parseInt(s, 8);
        return Number.isFinite(n) ? n : 0;
    }

    function parseString(bytes) {
        let end = 0;
        while (end < bytes.length && bytes[end] !== 0) end += 1;
        return new TextDecoder("utf-8").decode(bytes.subarray(0, end));
    }

    function parse(input) {
        const buf = input instanceof Uint8Array ? input : new Uint8Array(input);
        const out = [];
        let pos = 0;
        let pendingLongName = null;

        while (pos + BLOCK <= buf.length) {
            const header = buf.subarray(pos, pos + BLOCK);

            // Two consecutive zero blocks terminate the archive.
            let allZero = true;
            for (let i = 0; i < BLOCK; i++) {
                if (header[i] !== 0) { allZero = false; break; }
            }
            if (allZero) break;

            const name = parseString(header.subarray(0, 100));
            const size = parseOctal(header.subarray(124, 136));
            const typeflag = String.fromCharCode(header[156] || 0x30);
            const prefix = parseString(header.subarray(345, 500));
            pos += BLOCK;

            const dataLen = size;
            const padded = Math.ceil(dataLen / BLOCK) * BLOCK;

            if (typeflag === "L") {
                // GNU long name: next entry's name is the payload here.
                pendingLongName = parseString(buf.subarray(pos, pos + dataLen));
                pos += padded;
                continue;
            }

            if (typeflag === "0" || typeflag === "\u0000") {
                const fullName = pendingLongName || (prefix ? prefix + "/" + name : name);
                out.push({
                    name: fullName,
                    data: buf.slice(pos, pos + dataLen),
                });
            }
            pendingLongName = null;
            pos += padded;
        }
        return out;
    }

    root.Untar = { parse: parse };
})(typeof window !== "undefined" ? window : this);
