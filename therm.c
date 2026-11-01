/* therm — print Apple Silicon temperature sensor readings, one "name value" line each (°C).
 * Uses the private-but-stable IOHIDEventSystem API (same source as Stats / iStat Menus / `hot`).
 * No root needed. Build:  clang -O2 -o therm therm.c -framework IOKit -framework CoreFoundation
 * Used by noc_agent.py for Mac temps on macOS 26+, where powermetrics dropped the smc sampler. */
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>

typedef struct __IOHIDEventSystemClient *IOHIDEventSystemClientRef;
typedef struct __IOHIDServiceClient *IOHIDServiceClientRef;
typedef struct __IOHIDEvent *IOHIDEventRef;
extern IOHIDEventSystemClientRef IOHIDEventSystemClientCreate(CFAllocatorRef);
extern void IOHIDEventSystemClientSetMatching(IOHIDEventSystemClientRef, CFDictionaryRef);
extern CFArrayRef IOHIDEventSystemClientCopyServices(IOHIDEventSystemClientRef);
extern CFTypeRef IOHIDServiceClientCopyProperty(IOHIDServiceClientRef, CFStringRef);
extern IOHIDEventRef IOHIDServiceClientCopyEvent(IOHIDServiceClientRef, long, int, long);
extern double IOHIDEventGetFloatValue(IOHIDEventRef, int);

int main(void) {
    int usage = 5, page = 0xff00;                       /* sensor page: temp/humidity/etc. */
    CFNumberRef u = CFNumberCreate(NULL, kCFNumberIntType, &usage);
    CFNumberRef p = CFNumberCreate(NULL, kCFNumberIntType, &page);
    CFStringRef keys[2] = {CFSTR("PrimaryUsage"), CFSTR("PrimaryUsagePage")};
    CFTypeRef vals[2] = {u, p};
    CFDictionaryRef match = CFDictionaryCreate(NULL, (const void **)keys, (const void **)vals, 2,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    IOHIDEventSystemClientRef client = IOHIDEventSystemClientCreate(kCFAllocatorDefault);
    IOHIDEventSystemClientSetMatching(client, match);
    CFArrayRef svcs = IOHIDEventSystemClientCopyServices(client);
    if (!svcs) return 1;
    for (CFIndex i = 0; i < CFArrayGetCount(svcs); i++) {
        IOHIDServiceClientRef s = (IOHIDServiceClientRef)CFArrayGetValueAtIndex(svcs, i);
        CFTypeRef name = IOHIDServiceClientCopyProperty(s, CFSTR("Product"));
        IOHIDEventRef ev = IOHIDServiceClientCopyEvent(s, 15 /*kIOHIDEventTypeTemperature*/, 0, 0);
        if (!ev) { if (name) CFRelease(name); continue; }
        double v = IOHIDEventGetFloatValue(ev, 15 << 16 /*kIOHIDEventFieldTemperatureLevel*/);
        char nm[64] = "?";
        if (name && CFGetTypeID(name) == CFStringGetTypeID())
            CFStringGetCString(name, nm, sizeof nm, kCFStringEncodingUTF8);
        if (v > 0 && v < 150) printf("%s %.1f\n", nm, v);
        CFRelease(ev);
        if (name) CFRelease(name);
    }
    CFRelease(svcs);
    return 0;
}
