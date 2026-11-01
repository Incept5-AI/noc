/* fan — print Apple Silicon fan speeds, one "fan<N> <rpm>" line each.
 * Reads SMC keys via the AppleSMC IOKit user client (selector 2, no separate open call —
 * IOServiceOpen is enough). On Apple Silicon, RPM values are "flt " (IEEE float); FNum may
 * be absent, so keys are enumerated via #KEY + READ_INDEX and matched as F[0-9]Ac.
 * IMPORTANT: input and output must be SEPARATE structs (the driver's keyinfo reply
 * clobbers fields it validates on the next call) — sharing one buffer fails with
 * result 132/137. No root needed.
 * Build:  clang -O2 -o fan fan.c -framework IOKit -framework CoreFoundation
 * Used by noc_agent.py to show fan RPM next to the temperature on the NOC card.
 * Also prints "power <KEY> <W>": total system power from the first SMC key present of
 * PSTR / PDTR (laptops, matching SystemPowerIn) / PD0R (the M5 Ultra Studio's DC-in rail,
 * which has neither) — the only total-power source on an M5 desktop, whose IOReport rails
 * report GPU energy alone. */
#include <IOKit/IOKitLib.h>
#include <stdio.h>
#include <string.h>

typedef struct { char major, minor, build, reserved; UInt16 release; } vers_t;
typedef struct { UInt16 version, length; UInt32 cpuPLimit, gpuPLimit, memPLimit; } pLimit_t;
typedef struct { UInt32 dataSize, dataType; UInt8 dataAttributes; } keyInfo_t;
typedef struct {
    UInt32 key;
    vers_t vers;
    pLimit_t pLimitData;
    keyInfo_t keyInfo;
    UInt8 result, status, data8;
    UInt32 data32;
    UInt8 bytes[32];
} SMCKeyData;

enum { SMC_YPC = 2, SMC_READ_BYTES = 5, SMC_READ_INDEX = 8, SMC_READ_KEYINFO = 9 };

static io_connect_t conn;

static int smc_call(SMCKeyData *in, SMCKeyData *out) {
    size_t outsz = sizeof *out;
    return IOConnectCallStructMethod(conn, SMC_YPC, in, sizeof *in, out, &outsz) || out->result;
}

static int read_key(UInt32 key, SMCKeyData *val) {
    SMCKeyData in, out;
    memset(&in, 0, sizeof in);
    memset(&out, 0, sizeof out);
    in.key = key;
    in.data8 = SMC_READ_KEYINFO;
    if (smc_call(&in, &out)) return 1;
    memset(val, 0, sizeof *val);
    val->keyInfo = out.keyInfo;
    in.keyInfo.dataSize = out.keyInfo.dataSize;
    in.data8 = SMC_READ_BYTES;
    memset(&out, 0, sizeof out);
    if (smc_call(&in, &out)) return 1;
    memcpy(val->bytes, out.bytes, sizeof out.bytes);
    return 0;
}

static UInt32 fourcc(const char s[4]) {
    return ((UInt32)s[0] << 24) | ((UInt32)s[1] << 16) | ((UInt32)s[2] << 8) | (UInt32)s[3];
}

int main(void) {
    io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"));
    if (!svc) return 1;
    if (IOServiceOpen(svc, mach_task_self(), 0, &conn)) { IOObjectRelease(svc); return 1; }
    IOObjectRelease(svc);
    SMCKeyData val;
    if (read_key(fourcc("#KEY"), &val)) return 1;
    UInt32 total = ((UInt32)(UInt8)val.bytes[0] << 24) | ((UInt32)(UInt8)val.bytes[1] << 16)
                 | ((UInt32)(UInt8)val.bytes[2] << 8) | (UInt32)(UInt8)val.bytes[3];
    for (UInt32 i = 0; i < total; i++) {
        SMCKeyData in, out;
        memset(&in, 0, sizeof in);
        memset(&out, 0, sizeof out);
        in.data8 = SMC_READ_INDEX;
        in.data32 = i;
        if (smc_call(&in, &out)) continue;
        char k[5] = { out.key >> 24, out.key >> 16, out.key >> 8, out.key, 0 };
        if (!(k[0] == 'F' && k[1] >= '0' && k[1] <= '9' && k[2] == 'A' && k[3] == 'c'))
            continue;
        if (read_key(out.key, &val)) continue;
        float rpm;
        memcpy(&rpm, val.bytes, 4);
        printf("fan%c %.0f\n", k[1], rpm);
    }
    static const char *pkeys[] = { "PSTR", "PDTR", "PD0R" };
    for (int i = 0; i < 3; i++) {
        if (read_key(fourcc(pkeys[i]), &val) || val.keyInfo.dataType != fourcc("flt ")) continue;
        float w;
        memcpy(&w, val.bytes, 4);
        if (w > 0.5f) { printf("power %s %.1f\n", pkeys[i], w); break; }
    }
    IOServiceClose(conn);
    return 0;
}
