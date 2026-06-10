# monitoraeo production image — Railway picks this up automatically when
# present (overrides the default mise/Nixpacks build). We need a Dockerfile
# rather than nixpacks.toml because WeasyPrint requires native system
# libraries (Pango, GLib's libgobject, Cairo, etc.) that the previous
# Nix-based build wasn't actually making loadable to the Python runtime.
FROM python:3.11-slim-bookworm

# WeasyPrint native runtime deps — Pango+Cairo stack + GLib (libgobject)
# + fontconfig + harfbuzz for shaping + shared-mime-info for asset
# mime-type sniffing. Plus a small set of fonts so PDFs render with
# sensible text rather than tofu boxes when WeasyPrint hits a glyph
# the system fonts don't have.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libglib2.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz0b \
        libfontconfig1 \
        libffi-dev \
        shared-mime-info \
        fonts-dejavu-core \
        fonts-liberation \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so docker layer cache survives source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source.
COPY . .

# Railway sets $PORT at runtime; default to 8080 for local docker run.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn src.server:app --host 0.0.0.0 --port ${PORT}"]
