#!/usr/bin/env bash
# Wrapper to verify the LENS build
echo "Running LENS build verification..."
python3 verify_lens_build.py
if [ $? -eq 0 ]; then
    echo "Verification passed."
else
    echo "Verification failed."
    exit 1
fi
