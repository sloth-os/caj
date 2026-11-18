# syntax=docker/dockerfile:1
#
# caj2pdf image: the converter plus the CNKI CAJViewer runtime it drives.
#
# The runtime (libreaderex_x64.so + Resource/) is extracted from the vendored
# RPM at build time, so the image is self-contained: `docker run <image> x.caj`
# works with no mounts. See vendor/README.md for provenance and licensing.
#
# Build:  docker build -t caj2pdf .
# Run:    docker run --rm -v "$PWD:/work" -w /work caj2pdf doc.caj -o doc.pdf
# E2E:    docker run --rm --entrypoint python3 caj2pdf /app/tests/e2e_test.py

# -- stage 1: unpack the RPM (rpm2cpio+cpio are not in the slim runtime) -----
FROM debian:bookworm-slim AS unpack
RUN apt-get update \
 && apt-get install -y --no-install-recommends rpm2cpio cpio \
 && rm -rf /var/lib/apt/lists/*
COPY vendor/cajviewer-1.0-1.el7.noarch.rpm /tmp/cajviewer.rpm
# Payload root is opt/cajviewer/usr/{bin,lib,...}; extracting at / puts the
# runtime exactly where cajlib auto-discovers it.
RUN cd / && rpm2cpio /tmp/cajviewer.rpm | cpio -idm --quiet \
 && rm -f /tmp/cajviewer.rpm \
 && test -f /opt/cajviewer/usr/lib/libreaderex_x64.so

# -- stage 2: runtime --------------------------------------------------------
FROM python:3.12-slim

# libstdc++/libgcc for the vendored .so (libm/libc come with the base image)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libstdc++6 libgcc-s1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=unpack /opt/cajviewer /opt/cajviewer

# numpy + Pillow are the only third-party deps (no fontTools/reportlab/poppler)
RUN pip install --no-cache-dir 'numpy>=2' 'Pillow>=11'

WORKDIR /app
COPY caj2pdf.py ./
COPY cajpdf/ cajpdf/
COPY tests/ tests/
COPY README.md ./

ENV CAJREADER_LIB=/opt/cajviewer/usr/lib/libreaderex_x64.so \
    CAJVIEWER_DIR=/opt/cajviewer/usr/bin \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "caj2pdf.py"]
CMD ["--help"]
