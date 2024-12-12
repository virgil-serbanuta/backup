# Single line comment

if (FALSE) {
    "Multiline comment
    really"
}

myString <- "Hello, World!"

print(myString)

# Classes:
print("Classes:")
print("--------")
print(c("TRUE: ", class(TRUE)))
print(c("1: ", class(1)))
print(c("1.0:", class(1.0)))
print(c("1L:", class(1L)))
print(c("1+2i:", class(1+2i)))
print(c("1L+2i:", class(1L+2i)))
print(c("1L+1:", class(1L+1)))
print(c("\"a\":", class("a")))
print(c("charToRaw:", class(charToRaw("aoeu"))))
print(c("(1, 2, 3):", class(c(1, 2, 3))))

# Lists:
print("")
print("Lists:")
print("------")
print("list(1)", class(list(1)))
