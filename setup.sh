#!/bin/bash
# MRC emu — pre-build setup for Containerlab deployment
#
# Run this once on the server before 'containerlab deploy':
#   ./setup.sh
#
# It builds the mrc-emu Docker image and optionally imports the cEOS
# image if a tar file is present in the repo root.

set -e

echo "=== MRC emu setup ==="

# Build mrc-emu image
echo "Building mrc-emu Docker image..."
docker build -t mrc-emu:latest .
echo "✓ mrc-emu:latest built"

# Import cEOS if tar file exists
CEOS_TAR=$(ls cEOS*.tar 2>/dev/null | head -1)
if [ -n "$CEOS_TAR" ]; then
    echo "Importing cEOS from $CEOS_TAR..."
    docker import "$CEOS_TAR" ceos:4.36.0.1F
    echo "✓ ceos:4.36.0.1F imported"
else
    # Check if image already exists
    if docker image inspect ceos:4.36.0.1F >/dev/null 2>&1; then
        echo "✓ ceos:4.36.0.1F already available"
    else
        echo "⚠ No cEOS tar file found and image not available."
        echo "  Copy cEOS64-lab-4.36.0.1F.tar to this directory and re-run,"
        echo "  or import manually: docker import <file> ceos:4.36.0.1F"
    fi
fi

echo ""
echo "=== Setup complete ==="
echo "Deploy with: containerlab deploy -t topology.clab.yml"
