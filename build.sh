#!/bin/bash
# Build script for Cortex-PY Architecture Book

set -e

echo "Building Cortex-PY Architecture Book..."

cd source

# Clean previous build
rm -rf ../build

# Build HTML
sphinx-build -b html . ../build/html

echo "Build complete!"
echo "Open build/html/index.html to view the book."
