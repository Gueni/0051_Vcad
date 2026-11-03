// Analytic coverage for the retained WebGL layout primitives.
// tests/test_web_geometry.py checks parity, including the browser net color.
export const vertexShader = `#version 300 es
precision highp float;
precision highp int;
in vec2 worldPosition;
in vec2 localPosition;
in vec4 shapeParameters;
in vec4 sourceColor;
in uvec2 metadata;
in vec2 worldExpansion;
in vec2 localExpansion;
uniform vec2 camera;
uniform vec2 viewport;
uniform vec2 pixelMetrics;
uniform sampler2D objectFlags;
uniform int flagsWidth;
uniform int renderPass;
uniform int showPolygons;
out vec2 local;
flat out vec4 parameters;
flat out vec4 color;
flat out int shape;
flat out int selection;
void main() {
    int objectID = int(metadata.x);
    selection = int(texelFetch(objectFlags,
        ivec2(objectID % flagsWidth, objectID / flagsWidth), 0).r * 255.0 + 0.5);
    shape = int(metadata.y & 255u);
    parameters = shapeParameters;
    color = sourceColor;
    local = localPosition + localExpansion * pixelMetrics.y;
    vec2 world = worldPosition + worldExpansion * pixelMetrics.y;
    vec2 screen = (world - camera) * pixelMetrics.x;
    gl_Position = vec4(screen.x * 2.0 / viewport.x,
                       -screen.y * 2.0 / viewport.y, 0.0, 1.0);
    bool hole = (metadata.y & 512u) != 0u;
    if ((showPolygons == 0 && (metadata.y & 256u) != 0u)
        || (renderPass == 1 && (selection == 0 || hole))
        || (renderPass == 2 && !hole)) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0);
    }
}
`;
export const fragmentShader = `#version 300 es
precision highp float;
precision highp int;
in vec2 local;
flat in vec4 parameters;
flat in vec4 color;
flat in int shape;
flat in int selection;
uniform int renderPass;
uniform int selectionActive;
out vec4 outputColor;
const vec3 background = vec3(27.0, 29.0, 33.0) / 255.0;
float boxDistance(vec2 p, vec2 halfSize) {
    vec2 q = abs(p) - halfSize;
    return length(max(q, vec2(0.0))) + min(max(q.x, q.y), 0.0);
}
float ellipseDistance(vec2 p, vec2 radius) {
    radius = max(radius, vec2(1e-12));
    vec2 normalized = p / radius;
    float k = length(normalized);
    if (k < 1e-8) return -min(radius.x, radius.y);
    // Exact zero contour and unit gradient at the ellipse boundary. Filled
    // ellipses only need this local distance for one-pixel antialiasing.
    return (k - 1.0) * k / max(length(normalized / radius), 1e-12);
}
float distanceToShape() {
    if (shape == 1) {
        return length(vec2(max(abs(local.x) - parameters.x, 0.0), local.y))
               - parameters.y;
    }
    if (shape == 2) return boxDistance(local, parameters.xy);
    if (shape == 3) return boxDistance(local,
        vec2(parameters.x + parameters.y, parameters.y));
    if (shape == 4) return ellipseDistance(local, parameters.xy);
    if (shape == 5) {
        // Noncircular stroked ellipses are tessellated by the scene builder.
        return abs(length(local) - parameters.x) - parameters.z * 0.5;
    }
    if (shape == 6) {
        vec2 halfSize = parameters.xy;
        vec2 radius = min(max(parameters.zw, vec2(0.0)), halfSize);
        vec2 p = abs(local);
        if (min(radius.x, radius.y) > 0.0
            && p.x > halfSize.x - radius.x
            && p.y > halfSize.y - radius.y) {
            return ellipseDistance(p - (halfSize - radius), radius);
        }
        return max(p.x - halfSize.x, p.y - halfSize.y);
    }
    return -1.0;
}
float stripCoverage(float p, float radius, float pixelWidth) {
    pixelWidth = max(pixelWidth, 1e-12);
    return clamp(0.5 + (radius - p) / pixelWidth, 0.0, 1.0)
         - clamp(0.5 + (-radius - p) / pixelWidth, 0.0, 1.0);
}
float tinyCoverage(vec2 halfSize, float area, out bool tiny) {
    vec2 dx = dFdx(local), dy = dFdy(local);
    float determinant = dx.x * dy.y - dx.y * dy.x;
    tiny = false;
    if (abs(determinant) < 1e-20) return 0.0;
    vec2 axisX = vec2(dy.y, -dx.y) / determinant;
    vec2 axisY = vec2(-dy.x, dx.x) / determinant;
    vec2 extent = abs(axisX) * halfSize.x + abs(axisY) * halfSize.y;
    if (max(extent.x, extent.y) >= 0.5) return 0.0;
    tiny = true;
    vec2 offset = axisX * local.x + axisY * local.y;
    vec2 weight = max(vec2(1.0) - abs(offset), vec2(0.0));
    // A normalized tent retains projected area as a subpixel shape moves
    // between pixels, rather than giving every tiny shape half opacity.
    return clamp(area / abs(determinant), 0.0, 1.0) * weight.x * weight.y;
}
void main() {
    float coverage = 1.0;
    if (shape != 0) {
        float distance = distanceToShape();
        coverage = clamp(0.5 - distance / max(fwidth(distance), 1e-12), 0.0, 1.0);
        vec2 footprint = max(fwidth(local), vec2(1e-12));
        if (shape == 1) {
            float radius = parameters.y;
            float aa = max(fwidth(distance), footprint.y);
            coverage = clamp(0.5 - distance / aa, 0.0, 1.0)
                     - clamp(0.5 - (distance + 2.0 * radius) / aa, 0.0, 1.0);
            bool tiny;
            float small = tinyCoverage(vec2(parameters.x + radius, radius),
                4.0 * parameters.x * radius + 3.14159265359 * radius * radius, tiny);
            if (tiny) coverage = small;
        } else if (shape == 2 || shape == 3) {
            float halfLength = parameters.x + (shape == 3 ? parameters.y : 0.0);
            coverage = stripCoverage(local.x, halfLength, footprint.x)
                     * stripCoverage(local.y, parameters.y, footprint.y);
        } else if (shape == 4) {
            bool tiny;
            float small = tinyCoverage(parameters.xy,
                3.14159265359 * parameters.x * parameters.y, tiny);
            if (tiny) coverage = small;
        } else if (shape == 5) {
            float radius = parameters.x, halfWidth = parameters.z * 0.5;
            float radial = length(local);
            float aa = max(fwidth(radial), min(footprint.x, footprint.y));
            coverage = stripCoverage(radial - radius, halfWidth, aa);
            float outer = radius + halfWidth, inner = max(radius - halfWidth, 0.0);
            bool tiny;
            float small = tinyCoverage(vec2(outer),
                3.14159265359 * (outer * outer - inner * inner), tiny);
            if (tiny) coverage = small;
        } else if (shape == 6) {
            bool tiny;
            float area = 4.0 * parameters.x * parameters.y
                       - (4.0 - 3.14159265359) * parameters.z * parameters.w;
            float small = tinyCoverage(parameters.xy, area, tiny);
            if (tiny) coverage = small;
            else if (min(parameters.z, parameters.w) == 0.0) {
                coverage = stripCoverage(local.x, parameters.x, footprint.x)
                         * stripCoverage(local.y, parameters.y, footprint.y);
            }
        }
        if (coverage <= 0.0) discard;
    }
    vec3 rgb = color.rgb;
    if (renderPass == 0 && selectionActive != 0) {
        rgb = background + (rgb - background) * 0.15;
    } else if (renderPass == 1) {
        rgb = selection == 2 ? vec3(255.0, 224.0, 102.0) / 255.0
                             : vec3(104.0, 224.0, 255.0) / 255.0;
    }
    float alpha = color.a * coverage;
    outputColor = vec4(rgb * alpha, alpha);
}
`;
