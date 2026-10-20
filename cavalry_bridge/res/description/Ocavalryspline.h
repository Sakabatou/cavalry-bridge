#ifndef OCAVALRYSPLINE_H__
#define OCAVALRYSPLINE_H__

// Kept well clear of the IDs pulled in by Osplineprimitive (SPLINEOBJECT_*
// start at 1000, PRIM_* at 2300). 10100 is used privately for shape data.
enum
{
	CAVALRYSPLINE_LAYERS = 10000,
	CAVALRYSPLINE_SCALE  = 10001,   // 1.0.x only; still read from old scenes
	CAVALRYSPLINE_FOLLOW = 10002,
	CAVALRYSPLINE_WIDTH  = 10003,
	CAVALRYSPLINE_HEIGHT = 10004,
};

#endif
