/* pwr — print Apple Silicon power rails (mW), one "name value" line each.
 * Reads the IOReport "Energy Model" channels (same source as mactop/macmon); no root needed.
 * Works on macOS 26+ where powermetrics reports CPU Power: 0 mW for new chips.
 * Build:  clang -O2 -fblocks -o pwr pwr.c -framework CoreFoundation
 * Used by noc_agent.py, which prefers it over powermetrics when present next to the script. */
#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

typedef int (^IterBlock)(CFDictionaryRef);
static CFMutableDictionaryRef (*_ChannelsInGroup)(CFStringRef, CFStringRef, long, long, long);
static void *(*_CreateSubscription)(void *, CFMutableDictionaryRef, CFMutableDictionaryRef *, long, void *);
static CFDictionaryRef (*_CreateSamples)(void *, CFMutableDictionaryRef, void *);
static CFDictionaryRef (*_CreateSamplesDelta)(CFDictionaryRef, CFDictionaryRef, void *);
static void (*_Iterate)(CFDictionaryRef, IterBlock);
static CFStringRef (*_ChannelName)(CFDictionaryRef);
static CFStringRef (*_UnitLabel)(CFDictionaryRef);
static long long (*_SimpleInt)(CFDictionaryRef, int);

static double elapsed_s;

int main(void) {
    /* macOS 26 moved IOReport into libIOReport.dylib / PowerlogCore; older macOS has IOReport.framework */
    void *h = dlopen("/usr/lib/libIOReport.dylib", RTLD_LAZY);
    if (!h) h = dlopen("/System/Library/PrivateFrameworks/IOReport.framework/IOReport", RTLD_LAZY);
    if (!h) h = dlopen("/System/Library/PrivateFrameworks/PowerlogCore.framework/PowerlogCore", RTLD_LAZY);
    if (!h) return 1;
    void **fps[] = {(void **)&_ChannelsInGroup, (void **)&_CreateSubscription, (void **)&_CreateSamples,
                    (void **)&_CreateSamplesDelta, (void **)&_Iterate, (void **)&_ChannelName,
                    (void **)&_UnitLabel, (void **)&_SimpleInt};
    const char *syms[] = {"IOReportCopyChannelsInGroup", "IOReportCreateSubscription", "IOReportCreateSamples",
                          "IOReportCreateSamplesDelta", "IOReportIterate", "IOReportChannelGetChannelName",
                          "IOReportChannelGetUnitLabel", "IOReportSimpleGetIntegerValue"};
    for (int i = 0; i < 8; i++)
        if (!(*(fps[i]) = dlsym(h, syms[i]))) return 1;

    CFMutableDictionaryRef ch = _ChannelsInGroup(CFSTR("Energy Model"), NULL, 0, 0, 0);
    if (!ch) return 1;
    CFMutableDictionaryRef subbed = NULL;
    void *sub = _CreateSubscription(NULL, ch, &subbed, 0, NULL);
    if (!sub || !subbed) return 1;
    CFDictionaryRef s1 = _CreateSamples(sub, subbed, NULL);
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    nanosleep(&(struct timespec){1, 0}, NULL);                 /* energy delta over ~1s → mW */
    CFDictionaryRef s2 = _CreateSamples(sub, subbed, NULL);
    clock_gettime(CLOCK_MONOTONIC, &t1);
    elapsed_s = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
    CFDictionaryRef delta = _CreateSamplesDelta(s1, s2, NULL);
    if (!delta) return 1;
    _Iterate(delta, ^int(CFDictionaryRef smp) {
        CFStringRef nm = _ChannelName(smp);
        long long raw = _SimpleInt(smp, 0);
        char name[128] = "?", unit[32] = "";
        if (nm) CFStringGetCString(nm, name, sizeof name, kCFStringEncodingUTF8);
        CFTypeRef ul = _UnitLabel(smp);
        if (ul && CFGetTypeID(ul) == CFStringGetTypeID())
            CFStringGetCString(ul, unit, sizeof unit, kCFStringEncodingUTF8);
        /* scale energy delta to mW: mJ/s=mW, uJ and nJ need /1e3 and /1e6 */
        double scale = !strcmp(unit, "nJ") ? 1e-6 : !strcmp(unit, "uJ") ? 1e-3 : 1.0;
        double mw = raw * scale / elapsed_s;
        if (raw > 0 && mw < 1e6) printf("%s %.1f\n", name, mw);
        return 0;
    });
    return 0;
}
