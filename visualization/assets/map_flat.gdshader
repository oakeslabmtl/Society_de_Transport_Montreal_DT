shader_type spatial;

// The city: Godot's default material (white, fully rough), shaded flat. The map shares its vertices between
// triangles (4.7 times fewer than one per triangle corner, and it can then have levels of detail), so its
// vertex normals are smoothed across corners: each pixel takes its triangle's normal from the screen-space
// derivatives of its position instead

// The Compatibility (OpenGL) renderer offsets the sun's shadow lookups along the vertex normals: averaged across the
// buildings' corners, they left stripes and blotches on the roofs. Straight up instead (roofs and ground): none. The
// lighting uses the flat normal below
void vertex() {
	NORMAL = normalize(transpose(mat3(MODEL_MATRIX)) * vec3(0.0, 1.0, 0.0)); // World up once transformed, at any scale
}

void fragment() {
	vec3 normal = normalize(cross(dFdx(VERTEX), dFdy(VERTEX)));
	NORMAL = dot(normal, VERTEX) < 0.0 ? normal : -normal; // Toward the camera (the derivatives' sign differs per renderer)
	ALBEDO = vec3(1.0);
	ROUGHNESS = 1.0;
	METALLIC = 0.0;
	SPECULAR = 0.5;
}
