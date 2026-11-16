// link-settings: read and write an Insta360 Link camera's settings as JSON.
//
// cueclock's side-angle camera (docs/hardware.md Part F) keeps exposure, white
// balance, focus and the mic's noise suppression in the camera, and the only
// first-party way to reach them is Insta360's C++ SDK. Python cannot call C++
// classes, so this tiny helper is the bridge: `deck/sidecam.py` runs it and
// parses what it prints.
//
//   link-settings get                 -> {"camera": {...}, "settings": {...}, "ranges": {...}}
//   link-settings set key=value ...   -> {"applied": {key: true|false}, then the same as get}
//
// Exit codes: 0 ok, 1 usage error, 2 no Link camera attached.
//
// Built by scripts/fetch_insta360_sdk.sh against the SDK it downloads. The SDK
// itself is not in this repo (no published license); this file is ours.

#include <uvc_camera.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <chrono>
#include <map>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

using namespace uvc;

namespace {

constexpr uint16_t kInsta360VendorId = 0x2e1a;

std::string Escape(const std::string& s) {
    std::string out;
    for (char c : s) {
        if (c == '"' || c == '\\') out += '\\';
        if (static_cast<unsigned char>(c) >= 0x20) out += c;
    }
    return out;
}

// A JSON value as text; "null" when the camera did not answer.
using Json = std::string;
Json Null() { return "null"; }
Json Bool(bool v) { return v ? "true" : "false"; }
Json Int(long v) { return std::to_string(v); }
Json Float(float v) {
    std::ostringstream o;
    o << std::round(v * 100.0f) / 100.0f;
    return o.str();
}
Json Str(const std::string& v) { return "\"" + Escape(v) + "\""; }

std::string Object(const std::vector<std::pair<std::string, Json>>& fields) {
    std::string out = "{";
    for (size_t i = 0; i < fields.size(); ++i) {
        if (i) out += ", ";
        out += "\"" + fields[i].first + "\": " + fields[i].second;
    }
    return out + "}";
}

struct Camera {
    UVCCameraController std;
    UVCCameraExtendController ext;
    explicit Camera(const UVCCameraInfo& info) : std(info), ext(info) {}
};

// One setting: how to read it as JSON, and how to write it from text.
struct Setting {
    std::string key;
    std::function<Json(Camera&)> get;
    std::function<bool(Camera&, const std::string&)> set;
};

bool ParseBool(const std::string& v) { return v == "true" || v == "1"; }

bool ExtendStatus(Camera& c, ExtendFuction func, bool& out) {
    std::map<ExtendFuction, bool> status;
    if (!c.ext.GetExtendFuncStatus(status)) return false;
    auto it = status.find(func);
    if (it == status.end()) return false;
    out = it->second;
    return true;
}

// Pause after each write in `set` (why: at its use below).
constexpr auto kWriteGap = std::chrono::milliseconds(150);

// The order here is the order `set` applies them in: modes before the values
// they gate, so a manual shutter is not written while auto exposure still owns it.
std::vector<Setting> Settings() {
    return {
        {"video_mode",
         [](Camera& c) {
             VideoMode m;
             VideoModeAuxiliaryData aux;
             return c.ext.GetVideoMode(m, aux) ? Int(static_cast<long>(m)) : Null();
         },
         [](Camera& c, const std::string& v) {
             VideoModeAuxiliaryData aux;
             return c.ext.SetVideoMode(static_cast<VideoMode>(std::stol(v)), aux);
         }},
        {"hdr",
         [](Camera& c) {
             bool on;
             return ExtendStatus(c, ExtendFuction::HDR, on) ? Bool(on) : Null();
         },
         [](Camera& c, const std::string& v) { return c.ext.EnableExtendFuncWork(ExtendFuction::HDR, ParseBool(v)); }},
        {"auto_exposure",
         [](Camera& c) {
             bool on;
             return c.ext.GetAutoExposureStatus(on) || c.std.GetAutoExposureStatus(on) ? Bool(on) : Null();
         },
         // Extension first, matching the getter: on firmware v0.2.5.8 the
         // standard UVC call reports success and changes nothing (2026-11-15).
         [](Camera& c, const std::string& v) {
             return c.ext.EnableAutoExposure(ParseBool(v)) || c.std.EnableAutoExposure(ParseBool(v));
         }},
        {"wb_auto",
         [](Camera& c) {
             uint8_t mode;
             return c.std.GetWhitebalanceTemperatureMode(mode) ? Bool(mode == 1) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetWhitebalanceTemperatureMode(ParseBool(v) ? 1 : 0); }},
        {"autofocus",
         [](Camera& c) {
             bool on;
             return c.std.GetAutoFocusStatus(on) ? Bool(on) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.EnableAutoFocus(ParseBool(v)); }},
        // Before shutter: under anti-flicker Auto the camera refuses shutters
        // that are not a multiple of the mains period (1/240 s, 2026-11-15).
        {"anti_flicker",
         [](Camera& c) {
             PowerLineFrequency f;
             return c.std.GetPowerLineFrequency(f) ? Int(static_cast<long>(f)) : Null();
         },
         [](Camera& c, const std::string& v) {
             return c.std.SetPowerLineFrequency(static_cast<PowerLineFrequency>(std::stol(v)));
         }},
        {"shutter",
         [](Camera& c) {
             long s;
             return c.std.GetShutterSpeed(s) ? Int(s) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetShutterSpeed(std::stol(v)); }},
        {"iso",
         [](Camera& c) {
             uint16_t iso;
             return c.ext.GetISOValue(iso) ? Int(iso) : Null();
         },
         [](Camera& c, const std::string& v) { return c.ext.SetISOValue(static_cast<uint16_t>(std::stol(v))); }},
        {"ev",
         [](Camera& c) {
             float ev;
             return c.ext.GetExposureCompensation(ev) ? Float(ev) : Null();
         },
         [](Camera& c, const std::string& v) { return c.ext.SetExposureCompensation(std::stof(v)); }},
        {"wb_kelvin",
         [](Camera& c) {
             uint16_t k;
             return c.std.GetWhitebalanceTemperatureValue(k) ? Int(k) : Null();
         },
         [](Camera& c, const std::string& v) {
             return c.std.SetWhitebalanceTemperatureValue(static_cast<uint16_t>(std::stol(v)));
         }},
        {"focus",
         [](Camera& c) {
             long f;
             return c.std.GetFocusAbsolute(f) ? Int(f) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetFocusAbsolute(std::stol(v)); }},
        {"zoom",
         [](Camera& c) {
             long z;
             return c.std.GetZoomAbsolute(z) ? Int(z) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetZoomAbsolute(std::stol(v)); }},
        {"brightness",
         [](Camera& c) {
             int16_t b;
             return c.std.GetBrightnessValue(b) ? Int(b) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetBrightnessValue(static_cast<int16_t>(std::stol(v))); }},
        {"contrast",
         [](Camera& c) {
             uint16_t x;
             return c.std.GetContrastValue(x) ? Int(x) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetContrastValue(static_cast<uint16_t>(std::stol(v))); }},
        {"saturation",
         [](Camera& c) {
             uint16_t x;
             return c.std.GetSaturationValue(x) ? Int(x) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetSaturationValue(static_cast<uint16_t>(std::stol(v))); }},
        {"sharpness",
         [](Camera& c) {
             uint16_t x;
             return c.std.GetSharpnessValue(x) ? Int(x) : Null();
         },
         [](Camera& c, const std::string& v) { return c.std.SetSharpnessValue(static_cast<uint16_t>(std::stol(v))); }},
        {"noise_reduction",
         [](Camera& c) {
             bool on;
             return c.ext.GetAudioNoiseStatus(on) ? Bool(on) : Null();
         },
         [](Camera& c, const std::string& v) { return c.ext.EnableAudioNoiseReduction(ParseBool(v)); }},
    };
}

std::string Range(bool ok, const CameraControlInfo& r) {
    if (!ok) return Null();
    return Object({{"min", Int(r.min)}, {"max", Int(r.max)}, {"step", Int(r.step)}, {"default", Int(r.def)}});
}

std::string Report(Camera& c, const UVCCameraInfo& info) {
    DeviceInfo dev;
    bool have_dev = c.ext.GetDeviceInfo(dev);
    DeviceStatus status;
    bool have_status = c.ext.GetDeviceStatus(status);
    std::string camera = Object({
        {"name", Str(info.friendly_name)},
        {"id", Str(info.display_name)},
        {"firmware", have_dev ? Str(dev.hostFirmwareVer) : Null()},
        {"stream_open", have_status ? Bool(status.video_stream_is_opend) : Null()},
    });

    std::vector<std::pair<std::string, Json>> values;
    for (auto& s : Settings()) values.emplace_back(s.key, s.get(c));

    CameraControlInfo r;
    std::vector<long> shutters;
    std::string shutter_list = "null";
    if (c.std.GetShutterSpeedList(shutters)) {
        shutter_list = "[";
        for (size_t i = 0; i < shutters.size(); ++i) shutter_list += (i ? ", " : "") + Int(shutters[i]);
        shutter_list += "]";
    }
    std::string ranges = Object({
        {"shutter", shutter_list},
        {"wb_kelvin", Range(c.std.GetWhitebalanceTemperatureRange(r), r)},
        {"focus", Range(c.std.GetFocusAbsoluteRange(r), r)},
        {"zoom", Range(c.std.GetZoomAbsoluteRange(r), r)},
        {"brightness", Range(c.std.GetBrightnessRange(r), r)},
        {"contrast", Range(c.std.GetContrastRange(r), r)},
        {"saturation", Range(c.std.GetSaturationRange(r), r)},
        {"sharpness", Range(c.std.GetSharpnessRange(r), r)},
    });
    return "\"camera\": " + camera + ", \"settings\": " + Object(values) + ", \"ranges\": " + ranges;
}

}  // namespace

int main(int argc, char* argv[]) {
    if (argc < 2 || (std::string(argv[1]) != "get" && std::string(argv[1]) != "set")) {
        std::fprintf(stderr, "usage: link-settings get | set key=value ...\n");
        return 1;
    }
    std::vector<UVCCameraInfo> list;
    GetUVCCameraList(list);
    const UVCCameraInfo* found = nullptr;
    for (const auto& info : list) {
        if (info.vendor_id == kInsta360VendorId) {
            found = &info;
            break;
        }
    }
    if (found == nullptr) {
        std::fprintf(stderr, "no Insta360 Link camera attached\n");
        return 2;
    }
    Camera camera(*found);

    std::string applied;
    if (std::string(argv[1]) == "set") {
        std::map<std::string, std::string> wanted;
        for (int i = 2; i < argc; ++i) {
            std::string arg = argv[i];
            auto eq = arg.find('=');
            if (eq == std::string::npos) {
                std::fprintf(stderr, "expected key=value, got %s\n", argv[i]);
                return 1;
            }
            wanted[arg.substr(0, eq)] = arg.substr(eq + 1);
        }
        std::vector<std::pair<std::string, Json>> results;
        for (auto& s : Settings()) {
            auto it = wanted.find(s.key);
            if (it == wanted.end()) continue;
            bool ok = false;
            try {
                ok = s.set(camera, it->second);
            } catch (...) {  // std::stol on a malformed value
                ok = false;
            }
            results.emplace_back(s.key, Bool(ok));
            wanted.erase(it);
            // Firmware v0.2.5.8 silently drops a write that lands while it is
            // still taking the previous one: after video_mode/hdr/auto_exposure,
            // a back-to-back shutter and ISO were lost; 100 ms between writes
            // kept them (measured 2026-11-15).
            std::this_thread::sleep_for(kWriteGap);
        }
        if (!wanted.empty()) {
            std::fprintf(stderr, "unknown setting: %s\n", wanted.begin()->first.c_str());
            return 1;
        }
        applied = "\"applied\": " + Object(results) + ", ";
    }
    std::printf("{%s%s}\n", applied.c_str(), Report(camera, *found).c_str());
    return 0;
}
