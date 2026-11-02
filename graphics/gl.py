"""GPU 3D rendering via OpenGL 2.1 (legacy profile, Metal-backed on Apple Silicon) through ctypes.
Interface:
  gl            namespace of typed GL functions/constants used here (gl.glClear(gl.COLOR|gl.DEPTH), ...)
  GpuMesh(mesh) uploads a geometry.mesh.Mesh to GPU buffers (needs a current GL context);
                .draw(faces=True, edges=True, cap_color=None, program=None); .free()
                cap_color=(r,g,b) or "part": section-view mode — back faces are painted flat (unlit) in that colour or
                in each solid's own colour with a red tint, so with a clip plane
                the cut through closed, outward-wound solids looks capped (CAD section). Without a clip plane it only
                culls back faces.
                program=lit_program(...): draw through a GPU vertex-deformation shader (cap pass handled too)
  clip_plane((a,b,c,d), index=0) / clip_off(index=0)   user clip plane in the current object coordinates
  section_clip(cam, R, p, n_local)   cut the body at (R, p) through its own plane ⟂ n_local, removing the camera-facing half
  lit_program(deform_src) -> ShaderProgram   fixed-function-looking lighting + your GLSL
                `vec3 deform(vec3 p, inout vec3 n)` per vertex (e.g. a strain-wave gear's travelling deformation)
  OrbitCamera(fov=35)  CAD-style camera, Z up: .fit(bounds), .rotate(dx_px, dy_px), .pan(dx_px, dy_px, view_h_px),
                .zoom(factor) (>1 = closer), .reset(); .apply(w_px, h_px) sets viewport + matrices.
                fields: target, dist, yaw, pitch (degrees); .eye() -> camera position in world coordinates
  setup_scene(bg) / begin_frame(cam, w, h, bg)   lighting/depth state; clear + camera
  draw_axes(length)  RGB = XYZ axis lines at the origin;  draw_grid(size, step, z=0) light grid in the XY plane
  mult_pose(R, p)    multiply the current matrix by a rigid transform (place a body between glPushMatrix/glPopMatrix)
  offscreen_png(draw, w, h, path, samples=4)  render draw(w, h) in a hidden GPU context with MSAA → PNG.
                No window or screen needed; this is how GL output gets verified (then Read the PNG).
  ShaderProgram(vertex_src, fragment_src)  compile+link a GLSL program (raises RuntimeError with the driver log on
                failure; ValueError naming the line if a source has non-ASCII characters, which Apple's compiler rejects). .use(); .set(name, *floats|int) sets a uniform (1-4 floats, or a single int for sampler/bool);
                .set_vec3_array(name, [(x,y,z),...]) sets a uniform vec3[]; .loc(name) cached location; .free().
  fullscreen_quad()  draw a clip-space quad covering the viewport (pair with a vertex shader that passes gl_Vertex
                through to gl_Position); use this to run a fragment shader over every pixel (ray tracing, post-fx).
                Also sets gl_TexCoord[0] = (0..1, 0..1) across the quad.
  Target(w, h, samples=0, color=True, color_texture=False, depth_texture=False)  offscreen FBO in the current context:
                `with target:` binds it (restores the previous FBO + viewport); .read_rgb() / .read_depth(); .resolve(other)
  render_rgb(draw, w, h, samples=4) -> RGB bytes top-down (current context, else the hidden one)
  render_depth(draw, w, h, near, far) -> array('f') metric depth · render_ids(draw, w, h) / id_color(i) -> picking
  Camera(w, h, fov=None, fx, fy, cx, cy, near, far)  pinhole camera with intrinsics (OpenCV conventions): .look_at,
                .project/.unproject/.ray, .view_matrix/.proj_matrix, .apply(); .from_orbit(OrbitCamera)
  mat_mul / mat_pose / mat_ortho / gl_matrix   4x4 helpers (row-major lists; gl_matrix -> column-major ctypes)
  ShadowMap(size).update(center, radius, light, draw_casters) -> .matrix/.tex/.texel/.bias  directional shadow map
  program("id"|"outline"|"hud"), outline_pass(...), hud(draw_gfx, ...), hud_text(lines, ...) (2D text overlay via Gfx),
                draw_background(top, bottom)
  GpuMesh.draw_raw()  draw with the current shader/state (used by graphics/scene3d)
Lit, textured, shadowed scenes: graphics/scene3d.py (built on Target/Camera/ShadowMap here).
Used by: gui/window.py (gl=True windows), gui/glapp.py, graphics/scene3d.py, graphics/sky.py, graphics/texgen.py, gui/viewer3d.py, games (GPU fragment-shader games), robotics/armsim.py, robotics/roversim.py.
Fails: OSError if OpenGL can't load; RuntimeError if no accelerated pixel format / incomplete framebuffer / shader error.
Limits: OpenGL 2.1 / GLSL 1.20 (loops need constant bounds; no dynamic arrays). Fixed-function helpers coexist with
  shaders. Line width >1 may be ignored by the driver (Metal-backed GL caps lines at 1 px)."""
import ctypes
from ctypes import c_int, c_uint, c_float, c_double, c_void_p, c_char_p, c_ubyte, byref, POINTER, c_ssize_t
import math, types
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from macos.dylib import framework, cfunc
from geometry import vec as _vec
from geometry.projective import camera_matrix

_lib = framework("OpenGL")
gl = types.SimpleNamespace(
    COLOR=0x4000, DEPTH=0x100, DEPTH_TEST=0x0B71, LIGHTING=0x0B50, LIGHT0=0x4000, LIGHT1=0x4001, COLOR_MATERIAL=0x0B57,
    NORMALIZE=0x0BA1, POSITION=0x1203, DIFFUSE=0x1201, AMBIENT=0x1200, SPECULAR=0x1202, SHININESS=0x1601,
    LIGHT_MODEL_TWO_SIDE=0x0B52, LIGHT_MODEL_AMBIENT=0x0B53, FRONT_AND_BACK=0x0408, AMBIENT_AND_DIFFUSE=0x1602,
    SMOOTH=0x1D01, PROJECTION=0x1701, MODELVIEW=0x1700, TRIANGLES=4, LINES=1, FLOAT=0x1406, VERTEX_ARRAY=0x8074,
    NORMAL_ARRAY=0x8075, COLOR_ARRAY=0x8076, ARRAY_BUFFER=0x8892, STATIC_DRAW=0x88E4, POLYGON_OFFSET_FILL=0x8037,
    MULTISAMPLE=0x809D, LINE_SMOOTH=0x0B20, BLEND=0x0BE2, SRC_ALPHA=0x0302, ONE_MINUS_SRC_ALPHA=0x0303, RGBA=0x1908,
    UNSIGNED_BYTE=0x1401, FRAMEBUFFER=0x8D40, RENDERBUFFER=0x8D41, COLOR_ATTACHMENT0=0x8CE0, DEPTH_ATTACHMENT=0x8D00,
    RGBA8=0x8058, DEPTH_COMPONENT24=0x81A6, FRAMEBUFFER_COMPLETE=0x8CD5, READ_FRAMEBUFFER=0x8CA8, DRAW_FRAMEBUFFER=0x8CA9,
    NEAREST=0x2600, RENDERER=0x1F01, VERSION=0x1F02, LEQUAL=0x0203, QUADS=7,
    CULL_FACE=0x0B44, FRONT=0x0404, BACK=0x0405, CLIP_PLANE0=0x3000,
    FRAGMENT_SHADER=0x8B30, VERTEX_SHADER=0x8B31, COMPILE_STATUS=0x8B81, LINK_STATUS=0x8B82, INFO_LOG_LENGTH=0x8B84,
    TEXTURE_2D=0x0DE1, TEXTURE0=0x84C0, TEXTURE_MIN_FILTER=0x2801, TEXTURE_MAG_FILTER=0x2800, LINEAR=0x2601,
    TEXTURE_WRAP_S=0x2802, TEXTURE_WRAP_T=0x2803, CLAMP_TO_EDGE=0x812F, DEPTH_COMPONENT=0x1902, RGB=0x1907,
    UNPACK_ALIGNMENT=0x0CF5, PACK_ALIGNMENT=0x0D05, FRAMEBUFFER_BINDING=0x8CA6, VIEWPORT=0x0BA2, SCISSOR_TEST=0x0C11,
    NONE=0, DITHER=0x0BD0, TEXTURE_COMPARE_MODE=0x884C, ONE=1)

def _bind(name, res, *args, alias=None):
    f = getattr(_lib, name); f.restype = res; f.argtypes = list(args); setattr(gl, alias or name, f)

F, D, I, U, P = c_float, c_double, c_int, c_uint, c_void_p
for n, a in {"glClear": (U,), "glEnable": (U,), "glDisable": (U,), "glMatrixMode": (U,), "glLoadIdentity": (),
             "glClearColor": (F, F, F, F), "glViewport": (I, I, I, I), "glFrustum": (D,) * 6, "glTranslatef": (F, F, F),
             "glRotatef": (F, F, F, F), "glLightfv": (U, U, POINTER(F)), "glLightModeli": (U, I), "glLightModelfv": (U, POINTER(F)),
             "glColorMaterial": (U, U), "glMaterialfv": (U, U, POINTER(F)), "glMaterialf": (U, U, F), "glShadeModel": (U,),
             "glEnableClientState": (U,), "glDisableClientState": (U,), "glVertexPointer": (I, U, I, P), "glNormalPointer": (U, I, P),
             "glColorPointer": (I, U, I, P), "glDrawArrays": (U, I, I), "glGenBuffers": (I, POINTER(U)), "glDeleteBuffers": (I, POINTER(U)),
             "glBindBuffer": (U, U), "glBufferData": (U, c_ssize_t, P, U), "glLineWidth": (F,), "glPolygonOffset": (F, F),
             "glReadPixels": (I, I, I, I, U, U, P), "glFinish": (), "glDepthFunc": (U,), "glBlendFunc": (U, U), "glColor3f": (F, F, F), "glColor4f": (F, F, F, F),
             "glBegin": (U,), "glEnd": (), "glVertex3f": (F, F, F), "glGenFramebuffersEXT": (I, POINTER(U)),
             "glBindFramebufferEXT": (U, U), "glGenRenderbuffersEXT": (I, POINTER(U)), "glBindRenderbufferEXT": (U, U),
             "glRenderbufferStorageMultisampleEXT": (U, I, U, I, I), "glFramebufferRenderbufferEXT": (U, U, U, U),
             "glCheckFramebufferStatusEXT": (U,), "glBlitFramebufferEXT": (I, I, I, I, I, I, I, I, U, U),
             "glDeleteFramebuffersEXT": (I, POINTER(U)), "glDeleteRenderbuffersEXT": (I, POINTER(U)),
             "glPushMatrix": (), "glPopMatrix": (), "glMultMatrixf": (POINTER(F),), "glCullFace": (U,),
             "glClipPlane": (U, POINTER(D)), "glLoadMatrixf": (POINTER(F),), "glOrtho": (D,) * 6, "glTexCoord2f": (F, F),
             "glGenTextures": (I, POINTER(U)), "glDeleteTextures": (I, POINTER(U)), "glBindTexture": (U, U),
             "glTexImage2D": (U, I, I, I, I, I, U, U, P), "glTexParameteri": (U, U, I), "glActiveTexture": (U,),
             "glPixelStorei": (U, I), "glGetIntegerv": (U, POINTER(I)), "glScissor": (I, I, I, I), "glDrawBuffer": (U,),
             "glReadBuffer": (U,), "glFramebufferTexture2DEXT": (U, U, U, U, I), "glDepthMask": (U,),
             "glColorMask": (U, U, U, U), "glRenderbufferStorageEXT": (U, U, I, I)}.items():
    _bind(n, U if n == "glCheckFramebufferStatusEXT" else None, *a)
_bind("glGetString", c_char_p, U)
# --- GLSL shader entry points (OpenGL 2.0+, core in the macOS OpenGL framework) ---
_bind("glCreateShader", U, U)
_bind("glShaderSource", None, U, I, POINTER(c_char_p), POINTER(I))
_bind("glCompileShader", None, U)
_bind("glGetShaderiv", None, U, U, POINTER(I))
_bind("glGetShaderInfoLog", None, U, I, POINTER(I), c_char_p)
_bind("glDeleteShader", None, U)
_bind("glCreateProgram", U)
_bind("glAttachShader", None, U, U)
_bind("glLinkProgram", None, U)
_bind("glGetProgramiv", None, U, U, POINTER(I))
_bind("glGetProgramInfoLog", None, U, I, POINTER(I), c_char_p)
_bind("glUseProgram", None, U)
_bind("glDeleteProgram", None, U)
_bind("glGetUniformLocation", I, U, c_char_p)
_bind("glUniform1f", None, I, F); _bind("glUniform2f", None, I, F, F)
_bind("glUniform3f", None, I, F, F, F); _bind("glUniform4f", None, I, F, F, F, F)
_bind("glUniform1i", None, I, I); _bind("glUniform1fv", None, I, I, POINTER(F)); _bind("glUniform3fv", None, I, I, POINTER(F))
_bind("glUniformMatrix4fv", None, I, I, U, POINTER(F))
_bind("glUniformMatrix3fv", None, I, I, U, POINTER(F))
_lib.CGLGetCurrentContext.restype = c_void_p

def _fv(*v): return (F * len(v))(*v)

class GpuMesh:
    """A Mesh living in GPU buffers. Create/draw/free only while a GL context is current."""
    def __init__(self, mesh):
        self.ntri, self.nline = mesh.tris * 3, mesh.lines * 2
        ids = (U * 5)(); gl.glGenBuffers(5, ids); self.ids = ids
        for bid, arr in zip(ids, (mesh.tri_pos, mesh.tri_nrm, mesh.tri_col, mesh.line_pos, mesh.line_col)):
            gl.glBindBuffer(gl.ARRAY_BUFFER, bid)
            buf = (F * max(1, len(arr))).from_buffer(arr) if len(arr) else (F * 1)()
            gl.glBufferData(gl.ARRAY_BUFFER, len(arr) * 4, buf, gl.STATIC_DRAW)
        gl.glBindBuffer(gl.ARRAY_BUFFER, 0)

    def draw_raw(self):
        """Draw the triangles with whatever program/state is current: position, normal and colour arrays bound
        (gl_Vertex / gl_Normal / gl_Color), no lighting or polygon-offset changes. Used by shader renderers."""
        if not self.ntri: return
        b = self.ids
        gl.glEnableClientState(gl.VERTEX_ARRAY); gl.glEnableClientState(gl.NORMAL_ARRAY); gl.glEnableClientState(gl.COLOR_ARRAY)
        gl.glBindBuffer(gl.ARRAY_BUFFER, b[0]); gl.glVertexPointer(3, gl.FLOAT, 0, None)
        gl.glBindBuffer(gl.ARRAY_BUFFER, b[1]); gl.glNormalPointer(gl.FLOAT, 0, None)
        gl.glBindBuffer(gl.ARRAY_BUFFER, b[2]); gl.glColorPointer(3, gl.FLOAT, 0, None)
        gl.glDrawArrays(gl.TRIANGLES, 0, self.ntri)
        gl.glBindBuffer(gl.ARRAY_BUFFER, 0)
        gl.glDisableClientState(gl.VERTEX_ARRAY); gl.glDisableClientState(gl.NORMAL_ARRAY); gl.glDisableClientState(gl.COLOR_ARRAY)

    def draw(self, faces=True, edges=True, cap_color=None, program=None):
        if program is not None:                                         # lit_program(): GPU-deformed draw
            program.use(); program.set("u_mode", 0)
            if cap_color is not None and faces and self.ntri:
                gl.glEnable(gl.CULL_FACE); gl.glCullFace(gl.FRONT); program.set("u_mode", 1)
                self.draw(faces, False); gl.glCullFace(gl.BACK); program.set("u_mode", 0)
                self.draw(faces, edges); gl.glDisable(gl.CULL_FACE)
            else:
                self.draw(faces, edges)
            gl.glUseProgram(0)
            return
        if cap_color is not None and faces and self.ntri:
            # section caps: with a clip plane active, back faces seen through the cut are the inside of solid material;
            # paint them flat cap_color, then draw front faces normally (needs closed, outward-wound meshes)
            gl.glEnable(gl.CULL_FACE); gl.glCullFace(gl.FRONT); gl.glDisable(gl.LIGHTING)
            gl.glEnableClientState(gl.VERTEX_ARRAY)
            gl.glBindBuffer(gl.ARRAY_BUFFER, self.ids[0]); gl.glVertexPointer(3, gl.FLOAT, 0, None)
            if cap_color == "part":                                     # each solid's own colour, unlit (flat)
                gl.glEnableClientState(gl.COLOR_ARRAY)
                gl.glBindBuffer(gl.ARRAY_BUFFER, self.ids[2]); gl.glColorPointer(3, gl.FLOAT, 0, None)
            else:
                gl.glColor3f(*cap_color)
            gl.glDrawArrays(gl.TRIANGLES, 0, self.ntri)
            gl.glDisableClientState(gl.COLOR_ARRAY)
            if cap_color == "part":                                     # tint so cut faces read as cut faces
                gl.glEnable(gl.BLEND); gl.glBlendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.glColor4f(0.85, 0.12, 0.10, 0.38)
                gl.glDrawArrays(gl.TRIANGLES, 0, self.ntri); gl.glDisable(gl.BLEND)
            gl.glBindBuffer(gl.ARRAY_BUFFER, 0); gl.glDisableClientState(gl.VERTEX_ARRAY)
            gl.glCullFace(gl.BACK)
            self.draw(faces, edges)
            gl.glDisable(gl.CULL_FACE)
            return
        b = self.ids
        gl.glEnableClientState(gl.VERTEX_ARRAY); gl.glEnableClientState(gl.COLOR_ARRAY)
        if faces and self.ntri:
            gl.glEnable(gl.LIGHTING); gl.glEnableClientState(gl.NORMAL_ARRAY)
            gl.glEnable(gl.POLYGON_OFFSET_FILL); gl.glPolygonOffset(1.0, 1.0)   # keep edges visible on top
            gl.glBindBuffer(gl.ARRAY_BUFFER, b[0]); gl.glVertexPointer(3, gl.FLOAT, 0, None)
            gl.glBindBuffer(gl.ARRAY_BUFFER, b[1]); gl.glNormalPointer(gl.FLOAT, 0, None)
            gl.glBindBuffer(gl.ARRAY_BUFFER, b[2]); gl.glColorPointer(3, gl.FLOAT, 0, None)
            gl.glDrawArrays(gl.TRIANGLES, 0, self.ntri)
            gl.glDisableClientState(gl.NORMAL_ARRAY); gl.glDisable(gl.POLYGON_OFFSET_FILL)
        if edges and self.nline:
            gl.glDisable(gl.LIGHTING)
            gl.glBindBuffer(gl.ARRAY_BUFFER, b[3]); gl.glVertexPointer(3, gl.FLOAT, 0, None)
            gl.glBindBuffer(gl.ARRAY_BUFFER, b[4]); gl.glColorPointer(3, gl.FLOAT, 0, None)
            gl.glDrawArrays(gl.LINES, 0, self.nline)
        gl.glBindBuffer(gl.ARRAY_BUFFER, 0)
        gl.glDisableClientState(gl.VERTEX_ARRAY); gl.glDisableClientState(gl.COLOR_ARRAY)

    def free(self):
        gl.glDeleteBuffers(5, self.ids)

class OrbitCamera:
    def __init__(self, fov=35.0):
        self.fov = fov; self.reset()
    def reset(self):
        self.target, self.dist, self.yaw, self.pitch, self._fit = [0.0, 0.0, 0.0], 10.0, -35.0, 30.0, None
    def fit(self, bounds):
        (x0, y0, z0), (x1, y1, z1) = bounds
        self.target = [(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2]
        r = max(1e-6, math.dist((x0, y0, z0), (x1, y1, z1)) / 2)
        self.dist = r / math.sin(math.radians(self.fov / 2)) * 0.95; self._fit = bounds   # bounding sphere is conservative, so tighten it
    def rotate(self, dx, dy):
        self.yaw += dx * 0.4; self.pitch = max(-89.9, min(89.9, self.pitch + dy * 0.4))
    def pan(self, dx, dy, view_h):
        k = 2 * self.dist * math.tan(math.radians(self.fov / 2)) / max(1, view_h)   # world units per pixel
        yw, pt = math.radians(self.yaw), math.radians(self.pitch)
        right = (math.cos(yw), -math.sin(yw), 0.0)
        up = (math.sin(pt) * math.sin(yw), math.sin(pt) * math.cos(yw), math.cos(pt))
        for i in range(3): self.target[i] += (-dx * right[i] + dy * up[i]) * k
    def zoom(self, factor):
        self.dist = max(1e-4, self.dist / factor)
    def eye(self):
        """World position of the camera."""
        yw, pt = math.radians(self.yaw), math.radians(self.pitch)
        return [self.target[0] - self.dist * math.cos(pt) * math.sin(yw), self.target[1] - self.dist * math.cos(pt) * math.cos(yw),
                self.target[2] + self.dist * math.sin(pt)]
    def apply(self, w, h):
        gl.glViewport(0, 0, int(w), int(h))
        gl.glMatrixMode(gl.PROJECTION); gl.glLoadIdentity()
        near = self.dist * 0.01; t = near * math.tan(math.radians(self.fov / 2)); a = w / max(1, h)
        gl.glFrustum(-t * a, t * a, -t, t, near, self.dist * 100)
        gl.glMatrixMode(gl.MODELVIEW); gl.glLoadIdentity()
        gl.glLightfv(gl.LIGHT0, gl.POSITION, _fv(0.3, 0.5, 1.0, 0.0))     # headlight (eye space)
        gl.glLightfv(gl.LIGHT1, gl.POSITION, _fv(-0.6, -0.4, 0.3, 0.0))   # fill
        gl.glTranslatef(0, 0, -self.dist)
        gl.glRotatef(self.pitch - 90, 1, 0, 0)    # Z up
        gl.glRotatef(self.yaw, 0, 0, 1)
        gl.glTranslatef(*(-v for v in self.target))

def setup_scene():
    gl.glEnable(gl.DEPTH_TEST); gl.glDepthFunc(gl.LEQUAL); gl.glEnable(gl.MULTISAMPLE)
    gl.glEnable(gl.LIGHT0); gl.glEnable(gl.LIGHT1); gl.glEnable(gl.NORMALIZE); gl.glShadeModel(gl.SMOOTH)
    gl.glLightModeli(gl.LIGHT_MODEL_TWO_SIDE, 1); gl.glLightModelfv(gl.LIGHT_MODEL_AMBIENT, _fv(.28, .28, .3, 1))
    gl.glLightfv(gl.LIGHT0, gl.DIFFUSE, _fv(.8, .8, .8, 1)); gl.glLightfv(gl.LIGHT0, gl.SPECULAR, _fv(.35, .35, .35, 1))
    gl.glLightfv(gl.LIGHT1, gl.DIFFUSE, _fv(.3, .3, .35, 1))
    gl.glEnable(gl.COLOR_MATERIAL); gl.glColorMaterial(gl.FRONT_AND_BACK, gl.AMBIENT_AND_DIFFUSE)
    gl.glMaterialfv(gl.FRONT_AND_BACK, gl.SPECULAR, _fv(.4, .4, .4, 1)); gl.glMaterialf(gl.FRONT_AND_BACK, gl.SHININESS, 40.0)

def begin_frame(cam, w, h, bg=(.13, .14, .17)):
    setup_scene(); gl.glClearColor(*bg, 1.0); gl.glClear(gl.COLOR | gl.DEPTH); cam.apply(w, h)

def draw_axes(length):
    gl.glDisable(gl.LIGHTING); gl.glBegin(gl.LINES)
    for i, c in enumerate(((1, .3, .3), (.3, 1, .3), (.4, .5, 1))):
        gl.glColor3f(*c); gl.glVertex3f(0, 0, 0); gl.glVertex3f(*(length if j == i else 0 for j in range(3)))
    gl.glEnd()

def mult_pose(R, p):
    """Multiply the current matrix by the rigid transform with rotation R (3x3 rows) and translation p (3-vec).
    Use between glPushMatrix/glPopMatrix to place a body: the column-major 4x4 is [[R|p],[0 0 0 1]]."""
    M = (F * 16)(R[0][0], R[1][0], R[2][0], 0.0,
                 R[0][1], R[1][1], R[2][1], 0.0,
                 R[0][2], R[1][2], R[2][2], 0.0,
                 p[0], p[1], p[2], 1.0)
    gl.glMultMatrixf(M)

def clip_plane(eq, index=0):
    """Enable clip plane `index` keeping points with a·x+b·y+c·z+d >= 0, eq=(a,b,c,d) in the CURRENT modelview's object
    coordinates (call after placing a body to cut it in its own frame). Pair with GpuMesh.draw(cap_color=...)."""
    gl.glClipPlane(gl.CLIP_PLANE0 + index, (D * 4)(*eq)); gl.glEnable(gl.CLIP_PLANE0 + index)

def clip_off(index=0):
    gl.glDisable(gl.CLIP_PLANE0 + index)

def section_clip(cam, R, p, n_local, index=0):
    """CAD section view of the body placed at (R, p) (already multiplied onto the modelview): clip away the half of it
    facing the camera, through the plane in the body's frame with normal n_local passing through its origin."""
    eye = cam.eye(); nw = [sum(R[i][k] * n_local[k] for k in range(3)) for i in range(3)]
    s = 1.0 if sum(nw[i] * (eye[i] - p[i]) for i in range(3)) > 0 else -1.0
    clip_plane((-s * n_local[0], -s * n_local[1], -s * n_local[2], 0.0), index)

def draw_grid(size, step, z=0.0):
    gl.glDisable(gl.LIGHTING); gl.glBegin(gl.LINES); gl.glColor3f(.24, .25, .3)
    n = int(size / step)
    for i in range(-n, n + 1):
        gl.glVertex3f(i * step, -size, z); gl.glVertex3f(i * step, size, z)
        gl.glVertex3f(-size, i * step, z); gl.glVertex3f(size, i * step, z)
    gl.glEnd()

def _ascii_only(label, src):
    """Apple's GLSL compiler fails on any non-ASCII byte (even in a comment) with a misleading 'premature EOF'."""
    for i, line in enumerate(src.split("\n"), 1):
        bad = [c for c in line if ord(c) > 127]
        if bad: raise ValueError(f"{label} shader line {i} has non-ASCII {''.join(bad)!r} (GLSL must be ASCII): {line.strip()[:80]}")

def _compile(kind, src):
    sh = gl.glCreateShader(kind)
    buf = ctypes.create_string_buffer(src.encode())
    gl.glShaderSource(sh, 1, (c_char_p * 1)(ctypes.cast(buf, c_char_p)), (I * 1)(len(src)))
    gl.glCompileShader(sh)
    ok = I(); gl.glGetShaderiv(sh, gl.COMPILE_STATUS, byref(ok))
    if not ok.value:
        n = I(); gl.glGetShaderiv(sh, gl.INFO_LOG_LENGTH, byref(n))
        log = ctypes.create_string_buffer(max(1, n.value)); gl.glGetShaderInfoLog(sh, n.value, None, log)
        gl.glDeleteShader(sh)
        label = "vertex" if kind == gl.VERTEX_SHADER else "fragment"
        raise RuntimeError(f"{label} shader compile failed:\n{log.value.decode(errors='replace')}")
    return sh

class ShaderProgram:
    """A compiled+linked GLSL program. Build/use/free only while a GL context is current."""
    def __init__(self, vertex_src, fragment_src):
        _ascii_only("vertex", vertex_src); _ascii_only("fragment", fragment_src)
        vs, fs = _compile(gl.VERTEX_SHADER, vertex_src), _compile(gl.FRAGMENT_SHADER, fragment_src)
        self.id = gl.glCreateProgram()
        gl.glAttachShader(self.id, vs); gl.glAttachShader(self.id, fs); gl.glLinkProgram(self.id)
        ok = I(); gl.glGetProgramiv(self.id, gl.LINK_STATUS, byref(ok))
        if not ok.value:
            n = I(); gl.glGetProgramiv(self.id, gl.INFO_LOG_LENGTH, byref(n))
            log = ctypes.create_string_buffer(max(1, n.value)); gl.glGetProgramInfoLog(self.id, n.value, None, log)
            raise RuntimeError(f"program link failed:\n{log.value.decode(errors='replace')}")
        gl.glDeleteShader(vs); gl.glDeleteShader(fs)
        self._locs = {}

    def use(self): gl.glUseProgram(self.id)

    def loc(self, name):
        if name not in self._locs:
            self._locs[name] = gl.glGetUniformLocation(self.id, name.encode())
        return self._locs[name]

    def set(self, name, *vals):
        l = self.loc(name)
        if len(vals) == 1 and isinstance(vals[0], int): gl.glUniform1i(l, vals[0])
        else:
            v = [float(x) for x in vals]
            {1: gl.glUniform1f, 2: gl.glUniform2f, 3: gl.glUniform3f, 4: gl.glUniform4f}[len(v)](l, *v)

    def set_vec3_array(self, name, vecs):
        flat = [c for v in vecs for c in v]
        arr = (F * max(1, len(flat)))(*flat)
        gl.glUniform3fv(self.loc(name), len(vecs), arr)

    def free(self): gl.glDeleteProgram(self.id)

_LIT_VS = """
uniform int u_mode;   // 0 = lit like the fixed-function scene, 1 = section cap (flat part colour with a red tint)
%s
void main() {
    vec3 n = gl_Normal;
    vec3 p = deform(gl_Vertex.xyz, n);
    vec4 ec = gl_ModelViewMatrix * vec4(p, 1.0);
    gl_ClipVertex = ec;
    gl_Position = gl_ProjectionMatrix * ec;
    vec3 c = gl_Color.rgb;
    if (u_mode == 1) { gl_FrontColor = vec4(mix(c, vec3(0.85, 0.12, 0.10), 0.38), 1.0); return; }
    vec3 N = normalize(gl_NormalMatrix * n);
    vec3 col = gl_LightModel.ambient.rgb * c;
    for (int i = 0; i < 2; i++) {
        vec3 L = normalize(gl_LightSource[i].position.xyz);
        col += c * gl_LightSource[i].diffuse.rgb * abs(dot(N, L));
        vec3 Hh = normalize(L + vec3(0.0, 0.0, 1.0));
        col += gl_LightSource[i].specular.rgb * gl_FrontMaterial.specular.rgb * pow(abs(dot(N, Hh)), gl_FrontMaterial.shininess);
    }
    gl_FrontColor = vec4(col, 1.0);
}
"""
_LIT_FS = "void main() { gl_FragColor = gl_Color; }"

def lit_program(deform_src="vec3 deform(vec3 p, inout vec3 n) { return p; }"):
    """ShaderProgram that looks like the fixed-function scene (2 lights, two-sided, specular) but runs a user GLSL
    function `vec3 deform(vec3 p, inout vec3 n)` on every vertex (object coords) — GPU mesh deformation. Honors clip
    planes. Pass it to GpuMesh.draw(program=...); its cap pass switches u_mode automatically."""
    return ShaderProgram(_LIT_VS % deform_src, _LIT_FS)

def fullscreen_quad():
    """Draw a clip-space quad (-1..1) over the whole viewport. Bind a ShaderProgram whose vertex shader passes
    gl_Vertex straight to gl_Position (the fragment shader gets the pixel via gl_FragCoord or a varying UV)."""
    gl.glBegin(gl.QUADS)
    for x, y in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        gl.glVertex3f(float(x), float(y), 0.0)
    gl.glEnd()

_ctx = []
def _offscreen_context():
    if not _ctx:
        attrs = (c_int * 4)(73, 0)   # kCGLPFAAccelerated
        pix, n, ctx = c_void_p(), c_int(), c_void_p()
        _lib.CGLChoosePixelFormat.argtypes = [P, P, P]; _lib.CGLCreateContext.argtypes = [P, P, P]
        _lib.CGLSetCurrentContext.argtypes = [P]
        if _lib.CGLChoosePixelFormat(attrs, byref(pix), byref(n)) or not pix: raise RuntimeError("no accelerated GL pixel format")
        if _lib.CGLCreateContext(pix, None, byref(ctx)): raise RuntimeError("CGLCreateContext failed")
        _ctx.append(ctx)
    _lib.CGLSetCurrentContext(_ctx[0])

def context_key():
    """Opaque key of the current GL context (0 if none). GPU objects (buffers, textures, programs) belong to one
    context; cache them under this key."""
    return _lib.CGLGetCurrentContext() or 0

def ensure_context():
    """Make sure some GL context is current: keep the caller's (window on_draw), else use the hidden offscreen one."""
    if not context_key(): _offscreen_context()

def _flip_rows(data, w, h, bpp):
    stride = w * bpp
    return b"".join(data[y * stride:(y + 1) * stride] for y in range(h - 1, -1, -1))

def _check_size(w, h):
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0): raise ValueError(f"render size {w!r}x{h!r}")

class Target:
    """An offscreen framebuffer (FBO) in the CURRENT context: colour (RGBA8 renderbuffer, or a texture with
    color_texture=True) + 24-bit depth (renderbuffer, or a texture with depth_texture=True), optional MSAA samples.
    `with target:` binds it and sets the viewport, restoring the previous framebuffer + viewport on exit.
    read_rgb() -> bytes top-down RGB; read_depth() -> array('f') top-down window depth 0..1 (samples=0 only).
    resolve(other) blits colour (+depth) from an MSAA target into a single-sample one. free() releases it."""
    def __init__(self, w, h, samples=0, color=True, color_texture=False, depth_texture=False):
        _check_size(w, h)
        self.w, self.h, self.samples = w, h, samples
        self.fbo, self.rbs, self.texs = U(), [], []
        self.color_tex = self.depth_tex = None
        gl.glGenFramebuffersEXT(1, byref(self.fbo)); prev = self._binding(); gl.glBindFramebufferEXT(gl.FRAMEBUFFER, self.fbo)
        try:
            if color:
                if color_texture: self.color_tex = self._tex(gl.RGBA8, gl.RGBA, gl.UNSIGNED_BYTE, gl.COLOR_ATTACHMENT0)
                else: self._rb(gl.RGBA8, gl.COLOR_ATTACHMENT0)
            else:
                gl.glDrawBuffer(gl.NONE); gl.glReadBuffer(gl.NONE)
            if depth_texture: self.depth_tex = self._tex(gl.DEPTH_COMPONENT24, gl.DEPTH_COMPONENT, gl.FLOAT, gl.DEPTH_ATTACHMENT)
            else: self._rb(gl.DEPTH_COMPONENT24, gl.DEPTH_ATTACHMENT)
            st = gl.glCheckFramebufferStatusEXT(gl.FRAMEBUFFER)
            if st != gl.FRAMEBUFFER_COMPLETE: raise RuntimeError(f"framebuffer incomplete (0x{st:x})")
        finally:
            gl.glBindFramebufferEXT(gl.FRAMEBUFFER, prev)

    @staticmethod
    def _binding():
        v = I(); gl.glGetIntegerv(gl.FRAMEBUFFER_BINDING, byref(v)); return v.value

    def _rb(self, fmt, att):
        rb = U(); gl.glGenRenderbuffersEXT(1, byref(rb)); gl.glBindRenderbufferEXT(gl.RENDERBUFFER, rb)
        if self.samples: gl.glRenderbufferStorageMultisampleEXT(gl.RENDERBUFFER, self.samples, fmt, self.w, self.h)
        else: gl.glRenderbufferStorageEXT(gl.RENDERBUFFER, fmt, self.w, self.h)
        gl.glFramebufferRenderbufferEXT(gl.FRAMEBUFFER, att, gl.RENDERBUFFER, rb); self.rbs.append(rb)

    def _tex(self, ifmt, fmt, typ, att):
        if self.samples: raise ValueError("texture attachments need samples=0")
        t = _new_texture(gl.NEAREST); gl.glTexImage2D(gl.TEXTURE_2D, 0, ifmt, self.w, self.h, 0, fmt, typ, None)
        gl.glBindTexture(gl.TEXTURE_2D, 0)
        gl.glFramebufferTexture2DEXT(gl.FRAMEBUFFER, att, gl.TEXTURE_2D, t, 0); self.texs.append(t); return t.value

    def __enter__(self):
        vp = (I * 4)(); gl.glGetIntegerv(gl.VIEWPORT, vp)
        self._saved = (self._binding(), tuple(vp))
        gl.glBindFramebufferEXT(gl.FRAMEBUFFER, self.fbo); gl.glViewport(0, 0, self.w, self.h)
        return self

    def __exit__(self, *exc):
        fb, vp = self._saved; gl.glBindFramebufferEXT(gl.FRAMEBUFFER, fb); gl.glViewport(*vp)

    def resolve(self, other):
        prev = self._binding()
        gl.glBindFramebufferEXT(gl.READ_FRAMEBUFFER, self.fbo); gl.glBindFramebufferEXT(gl.DRAW_FRAMEBUFFER, other.fbo)
        gl.glBlitFramebufferEXT(0, 0, self.w, self.h, 0, 0, other.w, other.h, gl.COLOR, gl.NEAREST)
        gl.glBindFramebufferEXT(gl.FRAMEBUFFER, prev)

    def _read(self, fmt, typ, ctype, n, x=0, y=0, w=None, h=None):
        w = self.w if w is None else w; h = self.h if h is None else h
        prev = self._binding(); gl.glBindFramebufferEXT(gl.FRAMEBUFFER, self.fbo); gl.glFinish()
        gl.glPixelStorei(gl.PACK_ALIGNMENT, 1)
        buf = (ctype * (w * h * n))(); gl.glReadPixels(x, y, w, h, fmt, typ, buf)
        gl.glBindFramebufferEXT(gl.FRAMEBUFFER, prev)
        return buf

    def read_rgb(self, flip=True):
        """Colour as bytes, RGB, rows top-down (flip=False: GL's bottom-up order)."""
        raw = bytes(self._read(gl.RGB, gl.UNSIGNED_BYTE, c_ubyte, 3))
        return _flip_rows(raw, self.w, self.h, 3) if flip else raw

    def read_depth(self, flip=True):
        """Window-space depth 0..1 (1 = far plane / nothing drawn) as array('f'), rows top-down."""
        from array import array
        raw = bytes(self._read(gl.DEPTH_COMPONENT, gl.FLOAT, c_float, 1))
        return array("f", _flip_rows(raw, self.w, self.h, 4) if flip else raw)

    def free(self):
        n = len(self.rbs)
        if n: gl.glDeleteRenderbuffersEXT(n, (U * n)(*self.rbs))
        for t in self.texs: gl.glDeleteTextures(1, byref(t))
        gl.glDeleteFramebuffersEXT(1, byref(self.fbo)); self.rbs, self.texs = [], []

class Upscaler:
    _VS = "varying vec2 uv; void main(){ uv = gl_Vertex.xy * 0.5 + 0.5; gl_Position = gl_Vertex; }"
    _FS = "uniform sampler2D tex; varying vec2 uv; void main(){ gl_FragColor = vec4(texture2D(tex, uv).rgb, 1.0); }"

    def __init__(self): self.target, self.prog = None, None

    def draw(self, render, w, h, scale=0.5):
        if not 0.1 <= scale <= 1.0: raise ValueError("scale must be 0.1..1")
        rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
        if self.target is None or (self.target.w, self.target.h) != (rw, rh):
            if self.target: self.target.free()
            self.target = Target(rw, rh, color_texture=True)
            gl.glBindTexture(gl.TEXTURE_2D, self.target.color_tex)
            gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.glTexParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
            gl.glBindTexture(gl.TEXTURE_2D, 0)
        with self.target: result = render(rw, rh)
        if self.prog is None: self.prog = ShaderProgram(self._VS, self._FS)
        gl.glViewport(0, 0, int(w), int(h)); gl.glDisable(gl.DEPTH_TEST); gl.glDisable(gl.BLEND)
        self.prog.use(); gl.glActiveTexture(gl.TEXTURE0); gl.glBindTexture(gl.TEXTURE_2D, self.target.color_tex); self.prog.set("tex", 0)
        fullscreen_quad(); gl.glBindTexture(gl.TEXTURE_2D, 0); gl.glUseProgram(0)
        return result

    def free(self):
        if self.target: self.target.free(); self.target = None
        if self.prog: self.prog.free(); self.prog = None


def _new_texture(filt):
    t = U(); gl.glGenTextures(1, byref(t)); gl.glBindTexture(gl.TEXTURE_2D, t)
    for k, v in ((gl.TEXTURE_MIN_FILTER, filt), (gl.TEXTURE_MAG_FILTER, filt), (gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE),
                 (gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)):
        gl.glTexParameteri(gl.TEXTURE_2D, k, v)
    return t

def render_rgb(draw, w, h, samples=4):
    """Run draw(w, h) into an offscreen MSAA framebuffer and return the image as bytes: RGB, w*h*3, rows top-down.
    Uses the current GL context if any (e.g. inside a window's on_draw), else a hidden offscreen one."""
    _check_size(w, h); ensure_context()
    ms, out = Target(w, h, samples), Target(w, h, 0)
    try:
        with ms: draw(w, h); gl.glFinish()
        ms.resolve(out); return out.read_rgb()
    finally:
        ms.free(); out.free()

def linear_depth(d, near, far):
    """Perspective window depth d (0..1) -> eye-space distance along the view axis (metres if the scene is in metres).
    d >= 1 (background) -> inf."""
    if d >= 1.0: return math.inf
    zn = 2.0 * d - 1.0
    return 2.0 * near * far / ((far + near) - zn * (far - near))

def render_depth(draw, w, h, near, far):
    """Run draw(w, h) (which must set a perspective projection with these near/far planes, e.g. Camera.apply) and
    return z-depth per pixel as array('f') top-down: distance along the optical axis in scene units, inf where nothing
    was drawn. Resolution ≈ z²/near · 2⁻²⁴ (24-bit depth buffer), e.g. 5 µm at 2 m with near = 5 cm."""
    from array import array
    _check_size(w, h); ensure_context()
    t = Target(w, h, 0)
    try:
        with t:
            gl.glDisable(gl.MULTISAMPLE); draw(w, h); gl.glFinish()
        raw = t.read_depth()
    finally:
        t.free()
    return array("f", (linear_depth(d, near, far) for d in raw))

def id_color(i):
    """Flat colour (r,g,b floats) that encodes integer id i (0..2^24-1) exactly in an RGBA8 framebuffer."""
    if not 0 <= int(i) < 1 << 24: raise ValueError(f"id {i} outside 0..2^24-1")
    i = int(i); return ((i & 255) / 255.0, ((i >> 8) & 255) / 255.0, ((i >> 16) & 255) / 255.0)

def render_ids(draw, w, h):
    """Run draw(w, h) — which must paint every object in flat id_color(id) (no lighting, blending or MSAA) — into a
    single-sample framebuffer and decode it: array('I') top-down, one id per pixel, 0 = background."""
    from array import array
    _check_size(w, h); ensure_context()
    t = Target(w, h, 0)
    try:
        with t:
            gl.glDisable(gl.MULTISAMPLE); gl.glDisable(gl.DITHER); gl.glDisable(gl.BLEND)
            draw(w, h); gl.glFinish()
        rgb = t.read_rgb()
    finally:
        t.free(); gl.glEnable(gl.DITHER)
    return array("I", (rgb[k] | rgb[k + 1] << 8 | rgb[k + 2] << 16 for k in range(0, len(rgb), 3)))

def offscreen_png(draw, w, h, path, samples=4):
    from graphics.png import write_png
    _offscreen_context()
    write_png(path, w, h, render_rgb(draw, w, h, samples))
    return path

# ---------------------------------------------------------------------------------------------------------------
# 4x4 matrices (row-major nested lists) and the intrinsics camera
def mat_mul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)] for i in range(4)]

def mat_pose(R, p):
    """4x4 rigid transform [[R|p],[0 0 0 1]] from R (3x3 rows) and p."""
    return [[R[0][0], R[0][1], R[0][2], p[0]], [R[1][0], R[1][1], R[1][2], p[1]], [R[2][0], R[2][1], R[2][2], p[2]],
            [0.0, 0.0, 0.0, 1.0]]

def mat_ortho(l, r, b, t, n, f):
    return [[2 / (r - l), 0, 0, -(r + l) / (r - l)], [0, 2 / (t - b), 0, -(t + b) / (t - b)],
            [0, 0, -2 / (f - n), -(f + n) / (f - n)], [0, 0, 0, 1.0]]

def gl_matrix(M):
    """ctypes float[16] column-major (what glLoadMatrixf / glUniformMatrix4fv with transpose=0 expect)."""
    return (F * 16)(*(float(M[i][j]) for j in range(4) for i in range(4)))

def _v(a): return [float(x) for x in a]
def _vunit(a): return _vec.unit(a, "direction")
_vsub, _vcross = _vec.sub, _vec.cross

class Camera:
    """Pinhole camera with intrinsics, OpenCV conventions (what a real RGB-D sensor reports).
    Camera(w=640, h=480, fov=None, fx=None, fy=None, cx=None, cy=None, near=0.01, far=100.0)
      fov = vertical field of view in degrees (default 50) used when fx/fy are not given (fy = h/2 / tan(fov/2),
      fx = fy unless given). cx, cy default to the image centre ((w-1)/2, (h-1)/2). Pixel centres are at integer
      coordinates, (0,0) = centre of the top-left pixel; u right, v down.
    Camera frame: x right, y down, z forward (optical axis). Pose: .R (world-from-camera rotation, columns = camera
      axes in world), .t (camera centre in world). .look_at(eye, target, up=(0,0,1)) sets both and returns self.
    .K -> 3x3 intrinsic matrix; .fov_y -> degrees; .project(p) -> (u, v, z) for a world point (z = depth along the
      optical axis; None if z <= 0); .unproject(u, v, z) -> world point; .ray(u, v) -> (origin, unit dir);
      .view_matrix() / .proj_matrix() -> 4x4 GL matrices (row-major); .apply() sets viewport + both matrices so any
      GL drawing (fixed function or shaders) renders exactly what project() predicts.
      .resized(w, h) keeps the vertical fov, .crop(x, y, w, h) -> camera that renders just that pixel window (picking),
      .from_orbit(orbit, w, h) builds the same view as a gl.OrbitCamera. .eye() -> camera centre (like OrbitCamera).
    Fails: ValueError on non-positive size/focal length, near >= far, or a look_at with eye == target."""
    def __init__(self, w=640, h=480, fov=None, fx=None, fy=None, cx=None, cy=None, near=0.01, far=100.0):
        _check_size(w, h)
        if not 0 < near < far: raise ValueError(f"need 0 < near < far (got {near}, {far})")
        if fy is None:
            fy = fx if fx is not None and fov is None else h / 2.0 / math.tan(math.radians((fov or 50.0) / 2.0))
        if fx is None: fx = fy
        if fx <= 0 or fy <= 0: raise ValueError("focal lengths must be > 0")
        self.w, self.h, self.fx, self.fy = w, h, float(fx), float(fy)
        self.cx = (w - 1) / 2.0 if cx is None else float(cx)
        self.cy = (h - 1) / 2.0 if cy is None else float(cy)
        self.near, self.far = float(near), float(far)
        self.R = [[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]]; self.t = [0.0, 0.0, 1.0]   # looking down -Z by default

    def __repr__(self):
        return f"Camera({self.w}x{self.h}, fx={self.fx:.1f}, fy={self.fy:.1f}, cx={self.cx:.1f}, cy={self.cy:.1f}, t={self.t})"

    @property
    def K(self): return camera_matrix(self.fx,self.fy,self.cx,self.cy)
    @property
    def fov_y(self): return math.degrees(2 * math.atan(self.h / 2.0 / self.fy))

    def eye(self): return list(self.t)

    def look_at(self, eye, target, up=(0.0, 0.0, 1.0)):
        eye, target = _v(eye), _v(target)
        z = _vunit(_vsub(target, eye))
        x = _vcross(z, _v(up))
        if math.sqrt(sum(c * c for c in x)) < 1e-9: x = _vcross(z, [0.0, 1.0, 0.0] if abs(z[1]) < 0.9 else [1.0, 0.0, 0.0])
        x = _vunit(x); y = _vcross(z, x)
        self.R = [[x[i], y[i], z[i]] for i in range(3)]; self.t = eye
        return self

    def set_pose(self, R, t):
        """Set the camera pose directly: R world-from-camera (columns = x right, y down, z forward), t = centre."""
        self.R = [_v(r) for r in R]; self.t = _v(t); return self

    def to_camera(self, p):
        d = _vsub(_v(p), self.t); R = self.R
        return [R[0][i] * d[0] + R[1][i] * d[1] + R[2][i] * d[2] for i in range(3)]

    def project(self, p):
        x, y, z = self.to_camera(p)
        if z <= 0: return None
        return (self.fx * x / z + self.cx, self.fy * y / z + self.cy, z)

    def unproject(self, u, v, z):
        c = [(u - self.cx) / self.fx * z, (v - self.cy) / self.fy * z, z]; R = self.R
        return [self.t[i] + R[i][0] * c[0] + R[i][1] * c[1] + R[i][2] * c[2] for i in range(3)]

    def ray(self, u, v):
        return list(self.t), _vunit(_vsub(self.unproject(u, v, 1.0), self.t))

    def view_matrix(self):
        """World -> GL eye coordinates (x right, y up, looking down -z) = diag(1,-1,-1) · camera-from-world."""
        R, t = self.R, self.t
        rows = [[R[0][i], R[1][i], R[2][i]] for i in range(3)]                   # R^T
        tt = [-(rows[i][0] * t[0] + rows[i][1] * t[1] + rows[i][2] * t[2]) for i in range(3)]
        s = (1.0, -1.0, -1.0)
        return [[s[i] * rows[i][0], s[i] * rows[i][1], s[i] * rows[i][2], s[i] * tt[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]

    def proj_matrix(self):
        """GL projection from the intrinsics: pixel (u, v) of project() lands on the centre of window pixel
        (u, h-1-v) (window y is bottom-up)."""
        w, h, n, f = self.w, self.h, self.near, self.far
        cx, cy = self.cx + 0.5, self.cy + 0.5                                    # pixel-centre -> continuous coords
        return [[2 * self.fx / w, 0.0, 1 - 2 * cx / w, 0.0], [0.0, 2 * self.fy / h, 2 * cy / h - 1, 0.0],
                [0.0, 0.0, -(f + n) / (f - n), -2 * f * n / (f - n)], [0.0, 0.0, -1.0, 0.0]]

    def apply(self, viewport=True):
        if viewport: gl.glViewport(0, 0, self.w, self.h)
        gl.glMatrixMode(gl.PROJECTION); gl.glLoadMatrixf(gl_matrix(self.proj_matrix()))
        gl.glMatrixMode(gl.MODELVIEW); gl.glLoadMatrixf(gl_matrix(self.view_matrix()))

    def _copy(self, **kw):
        c = Camera.__new__(Camera); c.__dict__.update(self.__dict__); c.R = [list(r) for r in self.R]; c.t = list(self.t)
        c.__dict__.update(kw); return c

    def resized(self, w, h):
        """Same pose and vertical fov at a new image size (principal point re-centred if it was centred)."""
        _check_size(w, h)
        if (w, h) == (self.w, self.h): return self
        k = h / self.h; centred = abs(self.cx - (self.w - 1) / 2) < 1e-6 and abs(self.cy - (self.h - 1) / 2) < 1e-6
        return self._copy(w=w, h=h, fx=self.fx * k, fy=self.fy * k,
                          cx=(w - 1) / 2 if centred else self.cx * k, cy=(h - 1) / 2 if centred else self.cy * k)

    def crop(self, x, y, w=1, h=1):
        """Camera whose w x h image is the window of this camera's image starting at pixel (x, y) (top-left)."""
        return self._copy(w=w, h=h, cx=self.cx - x, cy=self.cy - y)

    @staticmethod
    def from_orbit(orbit, w, h):
        """Camera matching gl.OrbitCamera's view (same eye, target, vertical fov and near/far rule)."""
        c = Camera(int(w), int(h), fov=orbit.fov, near=orbit.dist * 0.01, far=orbit.dist * 100)
        eye = orbit.eye(); yw = math.radians(orbit.yaw)
        up = (0.0, 0.0, 1.0) if abs(orbit.pitch) < 89.0 else (math.sin(yw) * (1 if orbit.pitch > 0 else -1),
                                                               math.cos(yw) * (1 if orbit.pitch > 0 else -1), 0.0)
        return c.look_at(eye, orbit.target, up)

# ---------------------------------------------------------------------------------------------------------------
# Materials, lighting, shadow maps and the standard shaders
class ShadowMap:
    """Directional-light depth map. .update(center, radius, light, draw_casters) renders the casters seen from the
    light (any object with .direction = unit vector toward the light) into a size x size depth texture covering the
    sphere (center, radius); draw_casters(view_proj_4x4) must draw every shadow-casting mesh with that matrix
    (graphics/scene3d does). .matrix -> 4x4 world ->
    shadow texture coordinates (0..1) for the lit pass; .tex, .texel, .bias. Needs a current GL context."""
    def __init__(self, size=2048):
        if size < 64: raise ValueError("shadow map size must be >= 64")
        self.size = size; self.target = Target(size, size, 0, color=False, depth_texture=True)
        self.tex = self.target.depth_tex; self.matrix = None; self.texel = 1.0 / size; self.bias = 0.002

    def update(self, center, radius, light, draw_casters):
        r = max(float(radius), 1e-6); L = light.direction
        eye = [center[i] + L[i] * 2 * r for i in range(3)]
        cam = Camera(2, 2).look_at(eye, center, (0, 0, 1) if abs(L[2]) < 0.99 else (0, 1, 0))
        V = cam.view_matrix(); Pm = mat_ortho(-r, r, -r, r, 0.0, 4 * r); VP = mat_mul(Pm, V)
        with self.target:
            gl.glClear(gl.DEPTH); gl.glEnable(gl.DEPTH_TEST); gl.glDepthFunc(gl.LEQUAL)
            gl.glEnable(gl.POLYGON_OFFSET_FILL); gl.glPolygonOffset(1.5, 3.0); gl.glDisable(gl.CULL_FACE)
            draw_casters(VP)
            gl.glDisable(gl.POLYGON_OFFSET_FILL)
        bias = [[0.5, 0, 0, 0.5], [0, 0.5, 0, 0.5], [0, 0, 0.5, 0.5], [0, 0, 0, 1.0]]
        self.matrix = mat_mul(bias, VP)
        self.bias = 1.5 * (2 * r / self.size) / (4 * r)                        # ~1.5 texels of world depth
        return self

    def free(self): self.target.free()

_ID_VS = """#version 120
void main() { vec4 ec = gl_ModelViewMatrix * gl_Vertex; gl_ClipVertex = ec; gl_Position = gl_ProjectionMatrix * ec; }
"""
_ID_FS = "#version 120\nuniform vec4 u_idc;\nvoid main() { gl_FragColor = u_idc; }\n"
_QUAD_VS = """#version 120
uniform vec4 u_rect; uniform float u_flip; varying vec2 uv;
void main() { uv = gl_Vertex.xy * 0.5 + 0.5; gl_Position = vec4(mix(u_rect.xy, u_rect.zw, uv), 0.0, 1.0);
              if (u_flip > 0.5) uv.y = 1.0 - uv.y; }
"""
_OUTLINE_FS = """#version 120
uniform sampler2D u_tex; uniform vec2 u_px; uniform float u_r; uniform vec4 u_col; uniform vec4 u_hcol;
varying vec2 uv;
void main() {
    vec4 c = texture2D(u_tex, uv); float edge = 0.0; float hi = c.b;
    for (int i = 0; i < 8; i++) {
        float a = float(i) * 0.7853982;
        vec4 n = texture2D(u_tex, uv + vec2(cos(a), sin(a)) * u_r * u_px);
        if (any(greaterThan(abs(n.rgb - c.rgb), vec3(0.002)))) { edge = 1.0; hi = max(hi, n.b); }
    }
    if (edge < 0.5 || hi < 0.002) discard;
    gl_FragColor = hi > 0.006 ? u_hcol : u_col;
}
"""
_HUD_FS = """#version 120
uniform sampler2D u_b; uniform sampler2D u_w; varying vec2 uv;
void main() {
    vec3 b = texture2D(u_b, uv).rgb; vec3 w = texture2D(u_w, uv).rgb;
    float a = clamp(1.0 - dot(w - b, vec3(1.0 / 3.0)), 0.0, 1.0);
    if (a < 0.004) discard;
    gl_FragColor = vec4(b / max(a, 0.004), a);
}
"""
_progs = {}
def program(name):
    """Built-in shader program for the current context: "id" (flat u_idc colour), "outline", "hud". Compiled once
    per context. (Lit, textured, shadowed rendering lives in graphics/scene3d.py.)"""
    key = (context_key(), name)
    if key not in _progs:
        vs, fs = {"id": (_ID_VS, _ID_FS), "outline": (_QUAD_VS, _OUTLINE_FS),
                  "hud": (_QUAD_VS, _HUD_FS)}[name]
        _progs[key] = ShaderProgram(vs, fs)
    return _progs[key]

def _set_mat(prog, name, M): gl.glUniformMatrix4fv(prog.loc(name), 1, 0, gl_matrix(M))

def outline_pass(tex, w, h, radius=1.5, color=(0.05, 0.05, 0.07, 0.9), highlight=(1.0, 0.62, 0.1, 1.0)):
    """Draw outlines over the current framebuffer from an outline-id texture (w x h, GL row order): pixels whose
    neighbours within `radius` px have a different RGB, where one side has B > 0, get `color` (B = 1/255) or
    `highlight` (B = 2/255). graphics.scene3d builds the texture."""
    prog = program("outline"); prog.use()
    gl.glDisable(gl.DEPTH_TEST); gl.glEnable(gl.BLEND); gl.glBlendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)
    gl.glActiveTexture(gl.TEXTURE0); gl.glBindTexture(gl.TEXTURE_2D, tex)
    prog.set("u_tex", 0); prog.set("u_px", 1.0 / w, 1.0 / h); prog.set("u_r", float(radius))
    prog.set("u_col", *color); prog.set("u_hcol", *highlight); prog.set("u_rect", -1.0, -1.0, 1.0, 1.0); prog.set("u_flip", 0.0)
    fullscreen_quad()
    gl.glBindTexture(gl.TEXTURE_2D, 0); gl.glUseProgram(0); gl.glDisable(gl.BLEND); gl.glEnable(gl.DEPTH_TEST)

def draw_background(top, bottom):
    """Vertical gradient filling the viewport (no depth write). Call right after clearing."""
    gl.glUseProgram(0); gl.glDisable(gl.LIGHTING); gl.glDisable(gl.DEPTH_TEST); gl.glDepthMask(0)
    gl.glMatrixMode(gl.PROJECTION); gl.glPushMatrix(); gl.glLoadIdentity()
    gl.glMatrixMode(gl.MODELVIEW); gl.glPushMatrix(); gl.glLoadIdentity()
    gl.glBegin(gl.QUADS)
    for (x, y), c in zip(((-1, -1), (1, -1), (1, 1), (-1, 1)), (bottom, bottom, top, top)):
        gl.glColor3f(*c); gl.glVertex3f(float(x), float(y), 0.0)
    gl.glEnd()
    gl.glPopMatrix(); gl.glMatrixMode(gl.PROJECTION); gl.glPopMatrix(); gl.glMatrixMode(gl.MODELVIEW)
    gl.glDepthMask(1); gl.glEnable(gl.DEPTH_TEST)

# ---------------------------------------------------------------------------------------------------------------
# HUD: Gfx-drawn 2D overlays (any Gfx drawing, real system font, alpha) composited over a GL frame
_hud_cache = {}
def hud(draw_gfx, x, y, w, h, view_w, view_h, scale=1.0, key=None):
    """Composite a 2D Gfx drawing over the current GL framebuffer. draw_gfx(g) draws with graphics.appkit.AppKitGfx
    into a w x h box (points, top-left origin) placed at (x, y) in a view_w x view_h point viewport; scale = pixels per
    point (Retina: s.pw / s.w). Transparency is exact: the box is rendered on black and on white and the GPU recovers
    alpha. key: cache id — when the same key is passed again the cached texture is reused (no re-render), so pass
    e.g. the text being shown. Needs a current GL context."""
    if w <= 0 or h <= 0: return
    pw, ph = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    ck = (context_key(), key) if key is not None else None
    ent = _hud_cache.get(ck) if ck else None
    if ent is None or ent[0] != (pw, ph):
        from graphics.appkit import bitmap, AppKitGfx
        imgs = []
        for bg in (None, (1.0, 1.0, 1.0)):
            out = {}
            with bitmap(pw, ph, None, bg=bg, out=out, scale=scale): draw_gfx(AppKitGfx())
            imgs.append(out["rgb"])
        if ent: gl.glDeleteTextures(2, (U * 2)(*ent[1]))
        texs = []
        gl.glPixelStorei(gl.UNPACK_ALIGNMENT, 1)
        for data in imgs:
            t = _new_texture(gl.NEAREST); buf = (c_ubyte * len(data)).from_buffer_copy(data)
            gl.glTexImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, pw, ph, 0, gl.RGB, gl.UNSIGNED_BYTE, buf); texs.append(t.value)
        ent = ((pw, ph), texs)
        if ck:
            if len(_hud_cache) >= 64:                                 # evict this context's other entries, freeing their GPU textures
                for k in [k for k in _hud_cache if k[0] == ck[0] and k != ck]:
                    gl.glDeleteTextures(2, (U * 2)(*_hud_cache.pop(k)[1]))
            _hud_cache[ck] = ent
    prog = program("hud"); prog.use()
    gl.glDisable(gl.DEPTH_TEST); gl.glEnable(gl.BLEND); gl.glBlendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA)
    for unit, t in ((0, ent[1][0]), (1, ent[1][1])):
        gl.glActiveTexture(gl.TEXTURE0 + unit); gl.glBindTexture(gl.TEXTURE_2D, t)
    prog.set("u_b", 0); prog.set("u_w", 1); prog.set("u_flip", 1.0)
    x0, x1 = 2 * x / view_w - 1, 2 * (x + w) / view_w - 1; y0, y1 = 1 - 2 * (y + h) / view_h, 1 - 2 * y / view_h
    prog.set("u_rect", x0, y0, x1, y1)
    fullscreen_quad()
    gl.glActiveTexture(gl.TEXTURE0 + 1); gl.glBindTexture(gl.TEXTURE_2D, 0)
    gl.glActiveTexture(gl.TEXTURE0); gl.glBindTexture(gl.TEXTURE_2D, 0)
    if ck is None: gl.glDeleteTextures(2, (U * 2)(*ent[1]))
    gl.glUseProgram(0); gl.glDisable(gl.BLEND); gl.glEnable(gl.DEPTH_TEST)

def hud_text(lines, view_w, view_h, x=10, y=10, size=13, color=(1, 1, 1), panel=(0.0, 0.0, 0.0, 0.55), scale=1.0, pad=6):
    """Text panel overlay (HUD) at (x, y) points from the top-left; negative x / y anchor to the right / bottom edge.
    lines: str (newlines allowed) or list of str. Cached by content, so static text costs one texture draw."""
    if isinstance(lines, str): lines = lines.split("\n")
    lines = [str(t) for t in lines] or [""]
    from graphics.appkit import AppKitGfx
    try: tw = max(AppKitGfx().measure(t, size)[0] for t in lines)          # measure -> (w, h)
    except Exception: tw = max(len(t) for t in lines) * size * 0.6
    lh = size * 1.3; w, h = tw + 2 * pad, lh * len(lines) + 2 * pad
    if x < 0: x = view_w + x - w
    if y < 0: y = view_h + y - h
    def draw(g):
        if panel and panel[3] > 0: g.rect(0, 0, w, h, panel)
        for i, t in enumerate(lines): g.text(t, pad, pad + i * lh, size, color)
    hud(draw, x, y, w, h, view_w, view_h, scale, key=("text", tuple(lines), size, tuple(color), tuple(panel or ()), round(scale, 3)))
