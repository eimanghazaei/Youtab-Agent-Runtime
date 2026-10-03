import { rateLimit } from 'express-rate-limit';

// The bridge is loopback-only and single-process. Limit costly media sends at
// the HTTP boundary, before loading an arbitrary local file or invoking ffmpeg.
// A generous burst keeps normal gateway traffic working while bounding abuse.
export const mediaSendLimiter = rateLimit({
  windowMs: 60_000,
  limit: 60,
  standardHeaders: 'draft-7',
  legacyHeaders: false,
  message: { error: 'Too many media sends; retry shortly' },
});
