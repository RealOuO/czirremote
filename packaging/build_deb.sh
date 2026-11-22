#!/bin/bash
# 构建 arm64 deb —— 在 CardputerZero 上执行：bash packaging/build_deb.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PKG=cp0-ir-remote
VERSION=${VERSION:-1.1.2}
ARCH=${ARCH:-arm64}
STAGE="build/${PKG}"

rm -rf build
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/lib/${PKG}" \
         "$STAGE/usr/share/APPLaunch/applications" \
         "$STAGE/usr/share/APPLaunch/share/images" \
         "$STAGE/usr/share/doc/${PKG}"

install -m 644 src/miir.py            "$STAGE/usr/lib/${PKG}/miir.py"
install -m 644 src/kkac.py            "$STAGE/usr/lib/${PKG}/kkac.py"
install -m 644 src/cp0_pinyin.py      "$STAGE/usr/lib/${PKG}/cp0_pinyin.py"
install -m 644 src/cp0_ir_remote.py   "$STAGE/usr/lib/${PKG}/cp0_ir_remote.py"
install -m 644 assets/cp0-ir-remote.png "$STAGE/usr/share/APPLaunch/share/images/cp0-ir-remote.png"
install -m 644 packaging/cp0-ir-remote.desktop \
    "$STAGE/usr/share/APPLaunch/applications/cp0-ir-remote.desktop"
install -m 644 packaging/copyright    "$STAGE/usr/share/doc/${PKG}/copyright"

cat > "$STAGE/usr/bin/${PKG}" <<'LAUNCH'
#!/bin/sh
# APPLaunch 入口：原生 framebuffer 应用，无浏览器、无 HTTP 服务
exec /usr/bin/python3 /usr/lib/cp0-ir-remote/cp0_ir_remote.py "$@"
LAUNCH
chmod 755 "$STAGE/usr/bin/${PKG}"

find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -name "*.py" -exec chmod 644 {} +

SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
sed -e "s/@PKG@/${PKG}/" -e "s/@VERSION@/${VERSION}/" \
    -e "s/@ARCH@/${ARCH}/" -e "s/@SIZE@/${SIZE}/" \
    packaging/control.in > "$STAGE/DEBIAN/control"
chmod 644 "$STAGE/DEBIAN/control"

OUT="build/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT" >/dev/null
echo "built: $OUT  ($(du -h "$OUT" | cut -f1))"
echo "--- control ---"
dpkg-deb -f "$OUT" Package Version Architecture Depends
echo "--- files ---"
dpkg-deb -c "$OUT" | awk '{print $1, $6}'
