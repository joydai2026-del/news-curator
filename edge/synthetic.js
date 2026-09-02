// A synthetic private projection. Obviously fake, so this file can be public
// while the shape of a private record is still testable end to end.
//
// Nothing here is real story content, a real source, or a real reader.

export const SYNTHETIC_PROJECTION = Object.freeze({
  projection: 'synthetic_private_slate',
  synthetic: true,
  generated_for: 'edge_access_proof',
  tenant_id: 'tenant-owner-private',
  edition_date: '1970-01-01',
  entries: Object.freeze([
    Object.freeze({
      story_id: 'synthetic-story-0001',
      headline: 'SYNTHETIC PLACEHOLDER ALPHA',
      source: 'synthetic.invalid',
      lane: 'synthetic',
      rank: 1,
    }),
    Object.freeze({
      story_id: 'synthetic-story-0002',
      headline: 'SYNTHETIC PLACEHOLDER BRAVO',
      source: 'synthetic.invalid',
      lane: 'synthetic',
      rank: 2,
    }),
  ]),
});

export function syntheticProjectionFor(claims) {
  return {
    ...SYNTHETIC_PROJECTION,
    // Echo only that a subject was proven, never the subject itself.
    subject_present: Boolean(claims && (claims.sub || claims.email)),
  };
}
