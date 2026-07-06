"""Model aliases for the ported costing module.

The costing app's blueprints and engine were written against short class names
(Ingredient, Recipe, Setting...). Inside the pricing app those tables live as
Cost*-prefixed classes in models.py. This shim maps the old names onto the new
classes so the ported code reads exactly as it did in the standalone app —
one import line changed per file, nothing else.
"""
from models import (                                          # noqa: F401
    CostCategory as Category,
    CostSetting as Setting,
    CostIngredient as Ingredient,
    CostPriceHistory as PriceHistory,
    CostSpiceMix as SpiceMix,
    CostSpiceMixLine as SpiceMixLine,
    CostRecipe as Recipe,
    CostRecipeLine as RecipeLine,
    CostRecipeExtra as RecipeExtra,
    CostPackSize as PackSize,
    CostPackagingConfig as PackagingConfig,
    CostPackagingItem as PackagingItem,
    Carcass, CarcassCost, Cut,
    COSTING_SPECIES as SPECIES,
    utcnow,
)
