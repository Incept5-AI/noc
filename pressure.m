/* pressure — print the current thermal pressure level, one word.
 * Uses NSProcessInfo.thermalState: the documented Apple throttling signal, and the
 * same Nominal/Fair/Serious/Critical scale powermetrics reports — but public API,
 * so no root and no sudoers rule.
 * Build:  clang -O2 -o pressure pressure.m -framework Foundation */
#import <Foundation/Foundation.h>
#include <stdio.h>

int main(void) {
    @autoreleasepool {
        switch ([[NSProcessInfo processInfo] thermalState]) {
            case NSProcessInfoThermalStateNominal:  puts("Nominal");  break;
            case NSProcessInfoThermalStateFair:     puts("Fair");     break;
            case NSProcessInfoThermalStateSerious:  puts("Serious");  break;
            case NSProcessInfoThermalStateCritical: puts("Critical"); break;
            default: return 1;
        }
    }
    return 0;
}
