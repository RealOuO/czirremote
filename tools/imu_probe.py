#!/usr/bin/env python3
"""BMI270 / BMM150 probe for the CardputerZero (IIO sysfs, no root needed).

The on-board IMU is handled by the mainline ``bmi270_i2c`` driver, so the data
is already available as sysfs channel files.  Those are world-readable, which
is what makes an unprivileged app possible: ``/dev/iio:deviceN`` itself is
``root:root 0600``.

    python3 tools/imu_probe.py                # live, one line per poll
    python3 tools/imu_probe.py --seconds 5    # stop after 5 s
    python3 tools/imu_probe.py --csv out.csv   # append raw rows instead

Units come straight from the driver: accel m/s^2, anglvel rad/s (converted to
deg/s here), magn Gauss (converted to uT), temp milli-degC.
"""

import argparse
import math
import os
import sys
import time

IIO = "/sys/bus/iio/devices"
RAD2DEG = 180.0 / math.pi
GAUSS2UT = 100.0

ACCEL = ("in_accel_x_raw", "in_accel_y_raw", "in_accel_z_raw")
GYRO = ("in_anglvel_x_raw", "in_anglvel_y_raw", "in_anglvel_z_raw")
MAGN = ("in_magn_x_raw", "in_magn_y_raw", "in_magn_z_raw")


def find(name):
    """Return the sysfs directory of the first IIO device called ``name``."""
    try:
        entries = sorted(os.listdir(IIO))
    except OSError:
        return None
    for entry in entries:
        path = os.path.join(IIO, entry)
        try:
            with open(os.path.join(path, "name")) as handle:
                if handle.read().strip() == name:
                    return path
        except OSError:
            continue
    return None


def read(path, name, default=None):
    if not path:
        return default
    try:
        with open(os.path.join(path, name)) as handle:
            text = handle.read().strip()
    except OSError:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def vector(path, names, scale):
    """Raw triplet multiplied by ``scale``; None per axis if absent."""
    value = [read(path, n) for n in names]
    if any(v is None for v in value):
        return None
    return [v * scale for v in value]


def sample(imu, mag):
    accel_scale = read(imu, "in_accel_scale", 0.002394)
    gyro_scale = read(imu, "in_anglvel_scale", 0.001065) * RAD2DEG
    magn_scale = read(mag, "in_magn_scale", 0.000625) * GAUSS2UT
    return {
        "accel": vector(imu, ACCEL, accel_scale),
        "gyro": vector(imu, GYRO, gyro_scale),
        "magn": vector(mag, MAGN, magn_scale),
        "steps": read(imu, "in_steps_input"),
    }


def describe(row):
    accel, gyro, magn = row["accel"], row["gyro"], row["magn"]
    if accel:
        magnitude = math.sqrt(sum(v * v for v in accel))
        # Tilt of each axis away from "gravity on that axis", good enough to see
        # which way the board is held while the mounting is still unknown.
        tilt = [math.degrees(math.acos(max(-1.0, min(1.0, v / (magnitude or 1.0)))))
                for v in accel]
        accel_text = "%+6.2f %+6.2f %+6.2f  |g|=%5.2f  tilt x/y/z=%5.1f/%5.1f/%5.1f" % (
            *accel, magnitude, *tilt)
    else:
        accel_text = "accel: 不可用"
    gyro_text = ("gyro %+7.1f %+7.1f %+7.1f dps" % tuple(gyro)) if gyro else "gyro: 不可用"
    if magn:
        heading = math.degrees(math.atan2(magn[1], magn[0])) % 360.0
        magn_text = "mag %+7.1f %+7.1f %+7.1f uT  heading=%5.1f" % (*magn, heading)
    else:
        magn_text = "mag: 不可用（此批次无 BMM150）"
    return "  ".join((accel_text, gyro_text, magn_text))


def main():
    parser = argparse.ArgumentParser(description="BMI270 probe via IIO sysfs")
    parser.add_argument("--hz", type=float, default=10.0, help="poll rate (default 10)")
    parser.add_argument("--seconds", type=float, default=0.0, help="0 = run until Ctrl-C")
    parser.add_argument("--csv", help="append 't,ax,ay,az,gx,gy,gz,mx,my,mz' rows to this file")
    args = parser.parse_args()

    imu = find("bmi270")
    if not imu:
        print("找不到 bmi270 的 IIO 设备：确认 `lsmod | grep bmi270` 有输出", file=sys.stderr)
        return 1
    mag = find("bmm150")
    print("imu: %s" % imu)
    print("mag: %s" % (mag or "（无）"))
    print()

    period = 1.0 / max(args.hz, 0.1)
    start = time.monotonic()
    sink = open(args.csv, "a") if args.csv else None
    rows = 0
    try:
        while True:
            stamp = time.monotonic() - start
            row = sample(imu, mag)
            if sink and row["accel"] and row["gyro"]:
                values = list(row["accel"]) + list(row["gyro"]) + list(row["magn"] or (0, 0, 0))
                sink.write("%.3f,%s\n" % (stamp, ",".join("%.4f" % v for v in values)))
            if not sink:
                sys.stdout.write("\r%6.2fs  %s" % (stamp, describe(row)))
                sys.stdout.flush()
            rows += 1
            if args.seconds and stamp >= args.seconds:
                break
            time.sleep(max(0.0, period - ((time.monotonic() - start) - stamp)))
    except KeyboardInterrupt:
        pass
    finally:
        if sink:
            sink.close()
    if not sink:
        print()
    print("共采集 %d 组" % rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
