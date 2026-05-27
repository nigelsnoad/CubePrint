#!/bin/bash
# Build, and optionally sign + notarise, CubePrint.app
#
# Usage:
#   ./build.sh                        # build only
#   ./build.sh --sign                 # build + sign
#   ./build.sh --sign --notarise      # build + sign + notarise + staple
#
# Required env vars for signing/notarising:
#   SIGN_ID      Developer ID Application certificate name
#                e.g. "Developer ID Application: Jane Smith (ABCD1234EF)"
#   APPLE_ID     Your Apple ID email
#   APP_PASSWORD App-specific password from appleid.apple.com
#   TEAM_ID      Your 10-character team ID

set -euo pipefail

DO_SIGN=false
DO_NOTARISE=false
for arg in "$@"; do
    case $arg in
        --sign)      DO_SIGN=true ;;
        --notarise)  DO_NOTARISE=true; DO_SIGN=true ;;
    esac
done

echo "==> Installing / updating PyInstaller..."
.venv/bin/pip install pyinstaller --quiet

echo "==> Building CubePrint.app..."
.venv/bin/pyinstaller CubePrint.spec --clean --noconfirm

APP="dist/CubePrint.app"
echo "==> Built: $APP"

if $DO_SIGN; then
    : "${SIGN_ID:?Set SIGN_ID to your Developer ID Application certificate name}"

    echo "==> Signing $APP..."
    # Sign bt_rfcomm first (inner binaries must be signed before the bundle)
    codesign --force --verify --verbose \
        --sign "$SIGN_ID" \
        --options runtime \
        "$APP/Contents/Frameworks/bt_rfcomm"

    codesign --deep --force --verify --verbose \
        --sign "$SIGN_ID" \
        --options runtime \
        --entitlements entitlements.plist \
        "$APP"

    echo "==> Signed."
fi

if $DO_NOTARISE; then
    NOTARY_PROFILE="${NOTARY_PROFILE:-CubePrint}"

    ZIP="dist/CubePrint.zip"
    echo "==> Zipping for notarisation..."
    ditto -c -k --keepParent "$APP" "$ZIP"

    echo "==> Submitting to Apple for notarisation (this may take a minute)..."
    xcrun notarytool submit "$ZIP" \
        --keychain-profile "$NOTARY_PROFILE" \
        --wait

    echo "==> Stapling notarisation ticket..."
    xcrun stapler staple "$APP"

    echo "==> Notarised and stapled."
    rm "$ZIP"
fi

echo "==> Zipping for distribution..."
ditto -c -k --keepParent "$APP" "dist/CubePrint.zip"
echo "==> Done: $APP"
echo "==> Zip:  dist/CubePrint.zip"
