// wait until stdout's console output has left the UART
#include <stdio.h>
#include <termios.h>
#include <unistd.h>

// call tcdrain on stdout
int main(void) {
    if (tcdrain(STDOUT_FILENO) != 0) {
        perror("drain: tcdrain");
        return 1;
    }
    return 0;
}
