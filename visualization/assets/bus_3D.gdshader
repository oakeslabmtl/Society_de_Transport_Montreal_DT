shader_type spatial;
render_mode unshaded;

/*
	Depth-silhouette outline, as a next pass of the bus material. Derived from the Normal/Depth outline shader
	(Sobel edge detection on a normal and depth texture) written by William Li (LoudFlameLava), inspired by
	Yui Kinomoto @arlez80, lukky_nl (YT), Robin Seibold (YT).

	MIT License
*/

// Bus outline from the depth texture: its silhouette, where a neighboring pixel is clearly further away than the bus
// (the ground or a building behind it). "Clearly": more than the bus's own surface could slope over that many pixels,
// so the test holds at any distance. Depth only: the normal texture of the original shader made the whole city write
// its normals (up to 15.3 ms more on an Intel UHD, in the overview), and the Compatibility renderer does not have it.
// No repeat: a neighbor past the screen's edge is not read from the other side

uniform sampler2D DEPTH_TEXTURE : hint_depth_texture, repeat_disable, filter_nearest;
uniform vec3 outline_color: source_color;
uniform int outline_width = 1; // Pixels (the selected bus: 2, bus_3d.gd)

// Distance from the camera of what the depth texture holds at uv (scene units)
float get_depth(sampler2D depth_tex, vec2 uv, mat4 inv_projection_matrix) {
	float depth_raw = texture(depth_tex, uv).x;
#if CURRENT_RENDERER == RENDERER_COMPATIBILITY
	vec3 ndc = vec3(uv, depth_raw) * 2.0 - 1.0; // OpenGL: depth in [-1, 1]
#else
	vec3 ndc = vec3(uv * 2.0 - 1.0, depth_raw);
#endif
	vec4 view = inv_projection_matrix * vec4(ndc, 1.0);
	view.xyz /= view.w;
	float depth_linear = -view.z;
	return depth_linear;
}

void fragment() {
	vec2 pixel_size = vec2(1.0) / VIEWPORT_SIZE;
	float depth = -VERTEX.z; // This pass draws the bus again: the bus's distance at this pixel
	float slope = fwidth(depth); // How much the bus's surface moves away per pixel
	float jump = 0.0;
	for (int x = -outline_width; x <= outline_width; x++) {
		for (int y = -outline_width; y <= outline_width; y++) {
			jump = max(jump, get_depth(DEPTH_TEXTURE, SCREEN_UV + vec2(float(x), float(y)) * pixel_size, INV_PROJECTION_MATRIX) - depth);
		}
	}
	if (jump > 1.5 * float(outline_width + 1) * slope + 0.002 * depth) {
		ALBEDO = outline_color;
	} else {
		ALPHA = 0.0;
	}
}
