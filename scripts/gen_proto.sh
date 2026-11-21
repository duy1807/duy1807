#!/usr/bin/env bash
# Regenerate Yellowstone gRPC Python stubs from solbot/proto/*.proto
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m grpc_tools.protoc -I solbot/proto \
  --python_out=solbot/grpc_gen --grpc_python_out=solbot/grpc_gen \
  solbot/proto/geyser.proto solbot/proto/solana-storage.proto
sed -i 's/^import solana_storage_pb2 as/from . import solana_storage_pb2 as/; s/^from solana_storage_pb2 import \*/from .solana_storage_pb2 import */' solbot/grpc_gen/geyser_pb2.py
sed -i 's/^import geyser_pb2 as/from . import geyser_pb2 as/' solbot/grpc_gen/geyser_pb2_grpc.py
rm -f solbot/grpc_gen/solana_storage_pb2_grpc.py
echo "OK: stubs regenerated"
