#!/usr/bin/env bash
# setup_local_neo4j.sh, installs and starts a dedicated local Neo4j Community instance (no Docker).
#
# The instance lives in ~/neo4j-instances/tavily-provenance with its own data, its own ports
# (bolt 7699, http 7499) and its own password, so it never collides with Neo4j Desktop or any other
# Neo4j you already run on 7474/7687.
#
#   ./scripts/setup_local_neo4j.sh          install (first time) and start
#   ./scripts/setup_local_neo4j.sh stop     stop it
#   ./scripts/setup_local_neo4j.sh status   show whether it listens
#
# Needs Java 21+ on the PATH. Prefer Neo4j Aura instead? Skip this script and put the Aura URI in .env.
set -euo pipefail

VERSION="${NEO4J_VERSION:-2026.03.1}"
BASE="$HOME/neo4j-instances"
HOME_DIR="$BASE/tavily-provenance"
BOLT_PORT=7699
HTTP_PORT=7499
cmd="${1:-start}"

listening() { lsof -nP -iTCP:"$BOLT_PORT" -sTCP:LISTEN >/dev/null 2>&1; }

case "$cmd" in
  stop)
    [ -x "$HOME_DIR/bin/neo4j" ] && "$HOME_DIR/bin/neo4j" stop || echo "Not installed."
    ;;
  status)
    listening && echo "Listening on bolt $BOLT_PORT" || echo "Not running"
    ;;
  start)
    if [ ! -d "$HOME_DIR" ]; then
      mkdir -p "$BASE"
      echo "Downloading Neo4j Community $VERSION ..."
      curl -fsSL "https://dist.neo4j.org/neo4j-community-$VERSION-unix.tar.gz" | tar xz -C "$BASE"
      mv "$BASE/neo4j-community-$VERSION" "$HOME_DIR"
      cat >> "$HOME_DIR/conf/neo4j.conf" <<CONF

# aws-sample-tavily-provenance-graph: ports chosen to avoid Neo4j Desktop (7474/7687/7688)
server.bolt.listen_address=127.0.0.1:$BOLT_PORT
server.bolt.advertised_address=127.0.0.1:$BOLT_PORT
server.http.listen_address=127.0.0.1:$HTTP_PORT
server.http.advertised_address=127.0.0.1:$HTTP_PORT
CONF
      PW="$(openssl rand -base64 18 | tr -d '/+=')"
      "$HOME_DIR/bin/neo4j-admin" dbms set-initial-password "$PW" >/dev/null
      echo "$PW" > "$BASE/tavily-provenance.password"
      chmod 600 "$BASE/tavily-provenance.password"
    fi
    if listening; then echo "Already running on bolt $BOLT_PORT"; else
      "$HOME_DIR/bin/neo4j" start
      for _ in $(seq 1 30); do listening && break; sleep 2; done
      listening || { echo "Neo4j did not start, see $HOME_DIR/logs/neo4j.log" >&2; exit 1; }
    fi
    echo ""
    echo "Put these in 02-provenance-graph-demo/.env:"
    echo "  NEO4J_URI=neo4j://127.0.0.1:$BOLT_PORT"
    echo "  NEO4J_USER=neo4j"
    echo "  NEO4J_PASSWORD=$(cat "$BASE/tavily-provenance.password")"
    echo "Browser: http://127.0.0.1:$HTTP_PORT"
    ;;
  *) echo "usage: $0 [start|stop|status]" >&2; exit 2 ;;
esac
