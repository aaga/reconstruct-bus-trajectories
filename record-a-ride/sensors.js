// Sensor access: GPS (geolocation watch), accelerometer/gyro (devicemotion),
// and the screen Wake Lock. All three suspend when the tab is backgrounded —
// that's an OS-level fact we can't avoid — so app.js stops them cleanly on
// visibilitychange and restarts them on return; the IndexedDB record is the
// source of truth across the gap.
//
// iOS Safari quirk: DeviceMotionEvent.requestPermission() exists and MUST be
// called from inside a user gesture (a tap), otherwise 'devicemotion' fires
// with null values or not at all. requestMotionPermission() below is wired
// to the Start-recording tap in app.js.

// ------------------------------------------------------------------- GPS

/**
 * Start a high-accuracy geolocation watch. ~1 Hz is the practical browser
 * ceiling. Returns { stop() }.
 * onPing receives { t, lat, lon, accuracy, heading, speed } (heading/speed
 * may be null); onError receives the GeolocationPositionError.
 */
export function startGps(onPing, onError) {
  if (!("geolocation" in navigator)) {
    onError?.(new Error("Geolocation not supported on this device"));
    return { stop() {} };
  }
  const id = navigator.geolocation.watchPosition(
    (pos) => {
      const c = pos.coords;
      onPing({
        t: pos.timestamp,
        lat: c.latitude,
        lon: c.longitude,
        accuracy: c.accuracy ?? null,
        heading: Number.isFinite(c.heading) ? c.heading : null,
        speed: Number.isFinite(c.speed) ? c.speed : null,
      });
    },
    (err) => onError?.(err),
    // No `timeout`: a per-fix timeout fires TIMEOUT errors and, on some iOS
    // builds, stops the watch delivering — we'd rather keep waiting and show
    // staleness in the UI. `maximumAge: 0` forces fresh fixes; high accuracy
    // requests GPS, but the OS only honours it when Precise Location is on
    // (otherwise you get ~km-accuracy network fixes, refreshed every ~15 min).
    { enableHighAccuracy: true, maximumAge: 0 }
  );
  return {
    stop() {
      navigator.geolocation.clearWatch(id);
    },
  };
}

/** One-shot position, for the Setup view's nearby-stop search. */
export function getCurrentPosition() {
  return new Promise((resolve, reject) => {
    navigator.geolocation.getCurrentPosition(
      (pos) =>
        resolve({ lat: pos.coords.latitude, lon: pos.coords.longitude,
                  accuracy: pos.coords.accuracy }),
      reject,
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
    );
  });
}

// ------------------------------------------------------------ devicemotion

/**
 * Ask iOS for motion permission. Must run inside a user-gesture handler.
 * Resolves "granted" | "denied" | "unsupported" (non-iOS browsers don't
 * gate devicemotion, so "unsupported" still means motion will work).
 */
export async function requestMotionPermission() {
  if (typeof DeviceMotionEvent !== "undefined" &&
      typeof DeviceMotionEvent.requestPermission === "function") {
    try {
      return await DeviceMotionEvent.requestPermission();
    } catch {
      return "denied";
    }
  }
  return "unsupported";
}

/**
 * Start listening to devicemotion (~30–60 Hz). Returns { stop() }.
 * onSample receives { t, interval_ms, ax, ay, az, gx, gy, gz, agx, agy, agz }
 * where ax..az is gravity-removed linear acceleration when the device
 * provides it (falls back to accelerationIncludingGravity), gx..gz is
 * rotationRate in deg/s, and agx..agz is accelerationIncludingGravity —
 * recorded as its own stream because its low-passed direction IS the
 * per-frame gravity vector, the vertical reference the gravity-removed
 * channels can never supply (needed to sign lateral acceleration).
 */
export function startMotion(onSample) {
  const handler = (event) => {
    const a = event.acceleration?.x != null
      ? event.acceleration
      : event.accelerationIncludingGravity;
    if (!a || a.x == null) return; // desktop browsers fire empty events
    const r = event.rotationRate;
    const ag = event.accelerationIncludingGravity;
    onSample({
      t: Date.now(),
      interval_ms: event.interval ?? null,
      ax: a.x, ay: a.y, az: a.z,
      gx: r?.alpha ?? null, gy: r?.beta ?? null, gz: r?.gamma ?? null,
      agx: ag?.x ?? null, agy: ag?.y ?? null, agz: ag?.z ?? null,
    });
  };
  window.addEventListener("devicemotion", handler);
  return {
    stop() {
      window.removeEventListener("devicemotion", handler);
    },
  };
}

// --------------------------------------------------------------- WakeLock

/**
 * Keep the screen awake while recording. The lock is auto-released by the
 * browser when the tab is hidden; call acquire() again on return. Returns
 * { release() }; failures are non-fatal (older browsers).
 */
export async function acquireWakeLock() {
  let sentinel = null;
  try {
    sentinel = await navigator.wakeLock?.request("screen");
  } catch {
    // Denied or unsupported — recording still works, screen may sleep.
  }
  return {
    async release() {
      try {
        await sentinel?.release();
      } catch { /* already released */ }
      sentinel = null;
    },
  };
}
