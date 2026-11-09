#!/bin/sh
# Démarre en root pour donner le dossier de données au bon utilisateur, puis abandonne les privilèges.
set -e
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R "$PUID:$PGID" "$DATA_DIR" 2>/dev/null \
        || echo "Attention : impossible de changer le propriétaire de $DATA_DIR" >&2
    exec setpriv --reuid "$PUID" --regid "$PGID" --clear-groups "$@"
fi

if ! [ -w "$DATA_DIR" ]; then
    echo "Erreur : $DATA_DIR n'est pas accessible en écriture pour l'utilisateur $(id -u):$(id -g)." >&2
    echo "Corrige avec : chown -R $(id -u):$(id -g) <dossier monté sur $DATA_DIR>" >&2
    exit 1
fi
exec "$@"
