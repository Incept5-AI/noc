#!/bin/bash
# Build NOC and install it on a connected iPhone by signing it by hand, for when Xcode has no
# Apple account configured but you do have a development identity and a provisioning profile
# (e.g. a cached wildcard team profile) that lists the device. Normal route: open the project in
# Xcode with your team set and press Run.
#
#   DEVICE=<udid> IDENTITY=<sha1 of signing cert> PROFILE_UUID=<profile uuid> \
#   TEAM_ID=<team id> APP_ID_PREFIX=<app-id prefix> [BUNDLE_ID=com.incept5.noc] ./install-device.sh
#
# `xcrun devicectl list devices` gives the UDID; `security find-identity -v -p codesigning`
# gives the identity; profiles live in ~/Library/Developer/Xcode/UserData/Provisioning Profiles.
set -euo pipefail
: "${DEVICE:?set DEVICE}" "${IDENTITY:?set IDENTITY}" "${PROFILE_UUID:?set PROFILE_UUID}"
: "${TEAM_ID:?set TEAM_ID}" "${APP_ID_PREFIX:?set APP_ID_PREFIX}"
BUNDLE_ID=${BUNDLE_ID:-com.incept5.noc}
cd "$(dirname "$0")"
DD=${DD:-$(mktemp -d)}
xcodegen generate -q
xcodebuild -project NOC.xcodeproj -scheme NOC -configuration Release -destination "id=$DEVICE" \
  -derivedDataPath "$DD" CODE_SIGNING_ALLOWED=NO PRODUCT_BUNDLE_IDENTIFIER="$BUNDLE_ID" build -quiet
APP="$DD/Build/Products/Release-iphoneos/NOC.app"
cp "$HOME/Library/Developer/Xcode/UserData/Provisioning Profiles/$PROFILE_UUID.mobileprovision" "$APP/embedded.mobileprovision"
ENT="$DD/ent.plist"
cat > "$ENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>application-identifier</key><string>$APP_ID_PREFIX.$BUNDLE_ID</string>
<key>com.apple.developer.team-identifier</key><string>$TEAM_ID</string>
<key>get-task-allow</key><true/>
<key>keychain-access-groups</key><array><string>$APP_ID_PREFIX.$BUNDLE_ID</string></array>
</dict></plist>
PLIST
codesign --force --sign "$IDENTITY" --entitlements "$ENT" --timestamp=none --generate-entitlement-der "$APP"
xcrun devicectl device install app --device "$DEVICE" "$APP"
xcrun devicectl device process launch --device "$DEVICE" "$BUNDLE_ID"
