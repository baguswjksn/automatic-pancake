# wiki2epub — Wikipedia ke EPUB untuk Xteink X4

Konversi artikel Wikipedia menjadi EPUB yang ringan dan rapi di layar e-ink
Xteink X4 (4,3", 480×800, grayscale).

## Instalasi

```bash
python -m pip install -r requirements.txt
```

## Pemakaian

```bash
python wiki2epub.py https://id.wikipedia.org/wiki/Borobudur
python wiki2epub.py "Alan Turing" --lang en -o turing.epub
python wiki2epub.py URL1 URL2 URL3 --outdir buku/
```

Opsi:

| Opsi | Fungsi |
|---|---|
| `--lang id` | bahasa untuk judul biasa (bukan URL), default `id` |
| `-o FILE` | nama file output (hanya untuk satu artikel) |
| `--outdir DIR` | folder output |
| `--no-images` | tanpa gambar (file lebih kecil) |
| `--no-cover` | tanpa sampul otomatis |
| `--no-references` | buang daftar referensi |
| `--citations` | pertahankan penanda kutipan `[1]` (bisa diklik ke referensi) |

## Optimasi untuk X4

- Satu file XHTML per bagian (h2) → hemat RAM, halaman berpindah lebih cepat.
- Gambar diubah ke JPEG grayscale *baseline* (bukan progressive), maks 440×640 px.
- Infobox diratakan menjadi paragraf karena tabel lebar tidak muat di layar 480 px.
- Navbox, tautan edit, style, skrip, dan tautan wiki dibuang.
- Rumus matematika ditampilkan sebagai teks LaTeX.
- EPUB 3 + fallback NCX (EPUB 2), sampul 480×800 dibuat otomatis.